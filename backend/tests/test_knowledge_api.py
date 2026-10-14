"""Phase 6 API tests: tenant-scoped Knowledge Base CRUD over HTTP.

The database is real; only the embedding provider is substituted, at the same boundary as
in the service tests. These cover the parts that only exist at the API layer: the tenant
coming from the session rather than the body, the role bar on writes, and the cross-tenant
attack over actual endpoints.
"""

import uuid

import pytest

from app.core.config import get_settings
from app.models.membership import Role
from app.services.knowledge_base import KnowledgeBaseService
from tests.factories import add_member, create_tenant, create_user, issue_session

#: Captured once at import, before any test patches the attribute.
_ORIGINAL_INIT = KnowledgeBaseService.__init__

KB = "/api/v1/knowledge-base"


def _ai_settings(**overrides):
    base = {"embedding_enabled": True, "rag_min_similarity": -1.0}
    base.update(overrides)
    return get_settings().model_copy(update=base)


@pytest.fixture
async def world(owner):
    """Acme with an OWNER, plus a MEMBER, and a second tenant for the attack tests."""
    acme = await create_tenant(owner, name="Acme", slug="acme")
    owner_user = await create_user(owner, email="owner@acme.test", name="Ada")
    await add_member(owner, tenant_id=acme, user_id=owner_user, role=Role.OWNER)
    member = await create_user(owner, email="member@acme.test", name="Mo")
    await add_member(owner, tenant_id=acme, user_id=member, role=Role.MEMBER)

    globex = await create_tenant(owner, name="Globex", slug="globex")
    globex_user = await create_user(owner, email="owner@globex.test", name="Gia")
    await add_member(owner, tenant_id=globex, user_id=globex_user, role=Role.OWNER)

    return {
        "acme": acme,
        "globex": globex,
        "owner_headers": {
            "X-Tenant-ID": str(acme),
            "Cookie": f"qf_session={await issue_session(owner, user_id=owner_user)}",
        },
        "member_headers": {
            "X-Tenant-ID": str(acme),
            "Cookie": f"qf_session={await issue_session(owner, user_id=member)}",
        },
        "globex_headers": {
            "X-Tenant-ID": str(globex),
            "Cookie": f"qf_session={await issue_session(owner, user_id=globex_user)}",
        },
    }


@pytest.fixture
def ai_api(app, db, monkeypatch):
    """An API client whose endpoints see embeddings enabled.

    The service builds its own provider from configuration, and there is no injection
    point for that on the API path. Patching `KnowledgeBaseService.__init__` is narrower
    than dependency-overriding the router, and it leaves the router and the RAG context
    provider's own construction untouched - so the code under test is the real code.
    """
    import httpx

    from app.services.embeddings import HashingEmbeddingProvider
    from app.services.knowledge_base import KnowledgeBaseService

    settings = _ai_settings()
    provider = HashingEmbeddingProvider()

    def patched(self, session_factory, tenant_id, **kwargs):
        kwargs["provider"] = provider
        kwargs["settings"] = settings
        _ORIGINAL_INIT(self, session_factory, tenant_id, **kwargs)

    monkeypatch.setattr(KnowledgeBaseService, "__init__", patched)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def _doc(**overrides):
    body = {
        "title": "Pricing FAQ",
        "content": "Our pricing starts at 50000 rupees for a three van package.",
        "source_type": "faq",
        "metadata": {"owner": "sales"},
    }
    body.update(overrides)
    return body


# -- create ------------------------------------------------------------------


async def test_creating_a_document_chunks_and_stores_it(ai_api, world):
    async with ai_api as client:
        created = await client.post(KB, json=_doc(), headers=world["owner_headers"])
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["title"] == "Pricing FAQ"
        assert body["source_type"] == "faq"
        assert body["chunk_count"] == 1
        assert body["is_active"] is True
        assert "50000 rupees" in body["content"]
        assert body["metadata"] == {"owner": "sales"}
        # The vector is internal storage detail and must not cross the API boundary.
        assert "embedding" not in body
        assert not any("embedding" in key for key in body)


async def test_a_long_document_reports_its_chunk_count(ai_api, world):
    async with ai_api as client:
        created = await client.post(
            KB,
            json=_doc(content=" ".join(f"detail {i} about pricing" for i in range(400))),
            headers=world["owner_headers"],
        )
        assert created.status_code == 201
        assert created.json()["chunk_count"] > 1


async def test_empty_content_is_rejected(ai_api, world):
    async with ai_api as client:
        response = await client.post(
            KB,
            json={"title": "T", "content": "", "source_type": "faq"},
            headers=world["owner_headers"],
        )
    assert response.status_code == 422


async def test_an_unknown_source_type_is_rejected(ai_api, world):
    """The vocabulary is closed. `pdf`/`notion` are connectors, and this phase has none."""
    async with ai_api as client:
        response = await client.post(
            KB, json=_doc(source_type="notion"), headers=world["owner_headers"]
        )
    assert response.status_code == 422


async def test_a_tenant_id_in_the_body_is_rejected(ai_api, world):
    """There is no field to smuggle one into: `extra="forbid"` closes the door."""
    async with ai_api as client:
        response = await client.post(
            KB, json=_doc(tenant_id=str(world["globex"])), headers=world["owner_headers"]
        )
    assert response.status_code == 422


# -- read --------------------------------------------------------------------


async def test_listing_and_fetching_a_document(ai_api, world):
    async with ai_api as client:
        created = await client.post(KB, json=_doc(), headers=world["owner_headers"])
        document_id = created.json()["document_id"]

        listed = await client.get(KB, headers=world["owner_headers"])
        assert listed.status_code == 200
        assert listed.json()["count"] == 1
        assert listed.json()["items"][0]["document_id"] == document_id

        fetched = await client.get(f"{KB}/{document_id}", headers=world["owner_headers"])
        assert fetched.status_code == 200
        assert fetched.json()["document_id"] == document_id


async def test_an_unknown_document_is_a_404(ai_api, world):
    async with ai_api as client:
        response = await client.get(f"{KB}/{uuid.uuid4()}", headers=world["owner_headers"])
    assert response.status_code == 404


# -- update ------------------------------------------------------------------


async def test_updating_replaces_the_content(ai_api, world, db):
    from sqlalchemy import select

    from app.db.tenant_context import set_current_tenant
    from app.models.knowledge_base import KnowledgeBase

    async with ai_api as client:
        created = await client.post(KB, json=_doc(), headers=world["owner_headers"])
        document_id = created.json()["document_id"]

        updated = await client.put(
            f"{KB}/{document_id}",
            json=_doc(title="Warranty", content="Warranty is 24 months.", source_type="manual"),
            headers=world["owner_headers"],
        )
        assert updated.status_code == 200, updated.text
        assert "24 months" in updated.json()["content"]
        assert "50000" not in updated.json()["content"]

    # The old chunk is gone from storage, not merely hidden.
    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        rows = (
            (
                await session.execute(
                    select(KnowledgeBase).where(KnowledgeBase.document_id == document_id)
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert "24 months" in rows[0].content_chunk


async def test_updating_an_unknown_document_is_a_404(ai_api, world):
    async with ai_api as client:
        response = await client.put(
            f"{KB}/{uuid.uuid4()}", json=_doc(), headers=world["owner_headers"]
        )
    assert response.status_code == 404


# -- delete ------------------------------------------------------------------


async def test_deleting_a_document(ai_api, world):
    async with ai_api as client:
        created = await client.post(KB, json=_doc(), headers=world["owner_headers"])
        document_id = created.json()["document_id"]

        deleted = await client.delete(f"{KB}/{document_id}", headers=world["owner_headers"])
        assert deleted.status_code == 204

        assert (
            await client.get(f"{KB}/{document_id}", headers=world["owner_headers"])
        ).status_code == 404
        assert (await client.get(KB, headers=world["owner_headers"])).json()["count"] == 0


async def test_deleting_an_unknown_document_is_a_404(ai_api, world):
    async with ai_api as client:
        response = await client.delete(f"{KB}/{uuid.uuid4()}", headers=world["owner_headers"])
    assert response.status_code == 404


# -- authorization -----------------------------------------------------------


async def test_anonymous_requests_are_refused(ai_api, world):
    async with ai_api as client:
        assert (await client.get(KB)).status_code == 401
        assert (await client.post(KB, json=_doc())).status_code == 401


async def test_a_member_may_read_but_not_write(ai_api, world):
    """Writing knowledge changes what the AI tells every future lead, so it is an act."""
    async with ai_api as client:
        assert (await client.get(KB, headers=world["member_headers"])).status_code == 200
        response = await client.post(KB, json=_doc(), headers=world["member_headers"])
    assert response.status_code == 403


# -- the cross-tenant attack -------------------------------------------------


async def test_one_tenant_cannot_read_another_tenants_document(ai_api, world):
    async with ai_api as client:
        created = await client.post(KB, json=_doc(), headers=world["owner_headers"])
        document_id = created.json()["document_id"]

        response = await client.get(f"{KB}/{document_id}", headers=world["globex_headers"])
    assert response.status_code == 404


async def test_one_tenant_cannot_see_another_tenants_documents_in_a_listing(ai_api, world):
    async with ai_api as client:
        await client.post(KB, json=_doc(), headers=world["owner_headers"])
        listed = await client.get(KB, headers=world["globex_headers"])
    assert listed.status_code == 200
    assert listed.json()["count"] == 0


async def test_one_tenant_cannot_update_or_delete_another_tenants_document(ai_api, world):
    async with ai_api as client:
        created = await client.post(KB, json=_doc(), headers=world["owner_headers"])
        document_id = created.json()["document_id"]

        put = await client.put(f"{KB}/{document_id}", json=_doc(), headers=world["globex_headers"])
        delete = await client.delete(f"{KB}/{document_id}", headers=world["globex_headers"])

    assert put.status_code == 404
    assert delete.status_code == 404


async def test_the_owners_document_survives_the_attack(ai_api, world):
    async with ai_api as client:
        created = await client.post(KB, json=_doc(), headers=world["owner_headers"])
        document_id = created.json()["document_id"]
        await client.put(f"{KB}/{document_id}", json=_doc(), headers=world["globex_headers"])
        await client.delete(f"{KB}/{document_id}", headers=world["globex_headers"])

        still_there = await client.get(f"{KB}/{document_id}", headers=world["owner_headers"])
    assert still_there.status_code == 200
    assert "50000" in still_there.json()["content"]


async def test_a_non_member_of_the_target_tenant_is_refused(ai_api, world):
    """Acme's session asking for Globex's space, by tenant header alone."""
    async with ai_api as client:
        response = await client.get(
            KB,
            headers=world["owner_headers"],
        )
        assert response.status_code == 200
        # Now ask for Globex using Acme's credentials.
        crossed = await client.get(
            KB, headers={**world["owner_headers"], "X-Tenant-ID": str(world["globex"])}
        )
    assert crossed.status_code == 403
