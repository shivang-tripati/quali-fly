"""Meta connection admin API: tenant isolation, role authorization, secret redaction."""

import uuid

import pytest

from app.db.tenant_context import set_current_tenant
from app.models.membership import Role
from app.models.meta_connection import MetaConnection, MetaConnectionStatus
from tests.factories import (
    add_member,
    create_meta_connection,
    create_tenant,
    create_user,
    issue_session,
)
from tests.meta_fixtures import PAGE_A, PAGE_B, VERIFY_TOKEN

CONNECTS = "/api/v1/meta/connections"
SESSION_COOKIE = "qf_session"


@pytest.fixture
async def world(owner):
    tenant_a = await create_tenant(owner, name="Acme", slug="acme")
    tenant_b = await create_tenant(owner, name="Globex", slug="globex")
    admin = await create_user(owner, email="admin@dev.local", name="Admin")
    member = await create_user(owner, email="member@dev.local", name="Member")
    await add_member(owner, tenant_id=tenant_a, user_id=admin, role=Role.OWNER)
    await add_member(owner, tenant_id=tenant_a, user_id=member, role=Role.MEMBER)
    return {
        "a": tenant_a,
        "b": tenant_b,
        "admin": admin,
        "member": member,
        "conn_a": await create_meta_connection(
            owner, tenant_id=tenant_a, page_id=PAGE_A, verify_token=VERIFY_TOKEN
        ),
        "conn_b": await create_meta_connection(owner, tenant_id=tenant_b, page_id=PAGE_B),
    }


async def _as(api, owner, user_id, tenant_id):
    api.cookies.set(SESSION_COOKIE, await issue_session(owner, user_id=user_id))
    return {"X-Tenant-ID": str(tenant_id)}


# ------------------------------------------------------------------- auth


async def test_connection_api_requires_authentication(api, db, world):
    for method, path in (
        ("get", CONNECTS),
        ("post", CONNECTS),
    ):
        response = await getattr(api, method)(path)
        assert response.status_code == 401


async def test_member_cannot_create_a_connection(api, db, world, owner):
    headers = await _as(api, owner, world["member"], world["a"])

    response = await api.post(CONNECTS, json={"page_id": "12345"}, headers=headers)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ROLE_REQUIRED"


async def test_member_cannot_update_a_connection(api, db, world, owner):
    headers = await _as(api, owner, world["member"], world["a"])

    response = await api.patch(
        f"{CONNECTS}/{world['conn_a']}", json={"display_name": "hijacked"}, headers=headers
    )

    assert response.status_code == 403


async def test_member_can_read_connections(api, db, world, owner):
    headers = await _as(api, owner, world["member"], world["a"])

    response = await api.get(CONNECTS, headers=headers)

    assert response.status_code == 200
    assert [c["page_id"] for c in response.json()] == [PAGE_A]


async def test_owner_can_create_a_connection(api, db, world, owner):
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        CONNECTS,
        json={"page_id": "777777", "display_name": "Second page"},
        headers=headers,
    )

    assert response.status_code == 201
    body = response.json()
    assert body["page_id"] == "777777"
    assert body["provider"] == "META"
    assert body["status"] == "ACTIVE"


# ------------------------------------------------------------- secret hygiene


async def test_secrets_are_never_returned(api, db, world, owner):
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(f"{CONNECTS}/{world['conn_a']}", headers=headers)

    assert response.status_code == 200
    text = response.text
    # The token itself never appears, and the response has no field that could carry
    # one - only a boolean saying whether one is configured.
    assert VERIFY_TOKEN not in text
    body = response.json()
    assert body["webhook_verify_token_configured"] is True
    assert body["access_token_configured"] is False
    assert set(body) == {
        "id",
        "provider",
        "status",
        "display_name",
        "page_id",
        "instagram_business_id",
        "whatsapp_business_account_id",
        # Phase 10-O.3 adds the send address and its readable form. Both are identifiers,
        # not credentials - which is why the assertion above is that no *token* appears
        # rather than that these do.
        "whatsapp_phone_number_id",
        "whatsapp_display_number",
        "app_id",
        "webhook_verify_token_configured",
        "access_token_configured",
        # Phase 3 adds OAuth metadata. Still no credential material: `scopes` is what
        # we asked Meta for, not what it granted us a secret for.
        "scopes",
        "token_expires_at",
        "connected_at",
        "last_webhook_at",
        "created_at",
        "updated_at",
    }


async def test_a_supplied_verify_token_is_stored_as_a_digest(api, db, world, owner):
    """Regression: the token must survive the write allowlist and be hashed."""
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        CONNECTS,
        json={"page_id": "666666", "webhook_verify_token": "smoke-token-123"},
        headers=headers,
    )

    assert response.status_code == 201
    assert response.json()["webhook_verify_token_configured"] is True
    assert "smoke-token-123" not in response.text

    async with db() as session, session.begin():
        await set_current_tenant(session, world["a"])
        created = await session.get(MetaConnection, uuid.UUID(response.json()["id"]))
        assert created.webhook_verify_token_hash is not None
        assert "smoke-token-123" not in created.webhook_verify_token_hash


async def test_list_response_redacts_secrets_too(api, db, world, owner):
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(CONNECTS, headers=headers)

    assert VERIFY_TOKEN not in response.text


async def test_access_token_cannot_be_written_through_the_api(api, db, world, owner):
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(
        CONNECTS,
        json={"page_id": "888888", "access_token_encrypted": "leaked"},
        headers=headers,
    )

    assert response.status_code == 422  # extra="forbid"


# ------------------------------------------------------------- tenant scoping


async def test_list_only_returns_the_current_tenant(api, db, world, owner):
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(CONNECTS, headers=headers)

    assert [c["id"] for c in response.json()] == [str(world["conn_a"])]


async def test_another_tenants_connection_is_not_found_by_id(api, db, world, owner):
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(f"{CONNECTS}/{world['conn_b']}", headers=headers)

    assert response.status_code == 404


async def test_another_tenant_cannot_update_by_id(api, db, world, owner):
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.patch(
        f"{CONNECTS}/{world['conn_b']}", json={"display_name": "nope"}, headers=headers
    )

    assert response.status_code == 404

    async with db() as session, session.begin():
        await set_current_tenant(session, world["b"])
        untouched = await session.get(MetaConnection, world["conn_b"])
        assert untouched.display_name != "nope"


async def test_events_endpoint_is_tenant_scoped(api, db, world, owner):
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.get(f"{CONNECTS}/{world['conn_b']}/events", headers=headers)

    assert response.status_code == 404


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"page_id": "not-numeric"},
        {"display_name": "no identifier"},
    ],
)
async def test_invalid_create_payloads_are_rejected(api, db, world, owner, payload):
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(CONNECTS, json=payload, headers=headers)

    assert response.status_code == 422


# ------------------------------------------------- ambiguous source protection


async def test_two_active_connections_cannot_share_a_page_id(api, db, world, owner):
    other_admin = await create_user(owner, email="b-admin@dev.local", name="B Admin")
    await add_member(owner, tenant_id=world["b"], user_id=other_admin, role=Role.OWNER)
    headers = await _as(api, owner, other_admin, world["b"])

    response = await api.post(CONNECTS, json={"page_id": PAGE_A}, headers=headers)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "META_IDENTIFIER_CONFLICT"


async def test_the_same_page_id_can_be_reused_once_the_first_is_inactive(api, db, world, owner):
    other_admin = await create_user(owner, email="b-admin3@dev.local", name="B Admin 3")
    await add_member(owner, tenant_id=world["b"], user_id=other_admin, role=Role.OWNER)
    headers = await _as(api, owner, other_admin, world["b"])

    retired = await api.patch(
        f"{CONNECTS}/{world['conn_a']}", json={"status": "INACTIVE"}, headers=headers
    )
    # conn_a belongs to tenant A, so this must not succeed from tenant B.
    assert retired.status_code == 404

    async with db() as session, session.begin():
        await set_current_tenant(session, world["a"])
        connection = await session.get(MetaConnection, world["conn_a"])
        connection.status = MetaConnectionStatus.INACTIVE
        await session.flush()

    response = await api.post(CONNECTS, json={"page_id": PAGE_A}, headers=headers)
    assert response.status_code == 201


async def test_a_connection_needs_at_least_one_identifier(api, db, world, owner):
    headers = await _as(api, owner, world["admin"], world["a"])

    response = await api.post(CONNECTS, json={"display_name": "empty"}, headers=headers)

    assert response.status_code == 422
