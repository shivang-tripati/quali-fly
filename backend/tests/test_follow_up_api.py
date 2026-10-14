"""Phase 7 API: read-only task visibility, tenant-scoped.

The surface exists because without it a follow-up that silently does not send is
indistinguishable from one that was never scheduled. It is read-only on purpose - a write
endpoint here would be a second path to mutate task state outside the scheduler, which is
the kind of second path that makes a state machine ambiguous.
"""

import uuid

import pytest

from app.models.membership import Role
from tests.factories import add_member, create_tenant, create_user, issue_session
from tests.follow_up_fixtures import make_conversation, service

TASKS = "/api/v1/scheduled-tasks"


@pytest.fixture
async def world(owner):
    """Acme with an OWNER and a MEMBER, plus a second tenant for the attack tests."""
    acme = await create_tenant(owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    owner_user = await create_user(owner, email="owner@acme.test", name="Ada")
    await add_member(owner, tenant_id=acme, user_id=owner_user, role=Role.OWNER)
    member = await create_user(owner, email="member@acme.test", name="Mo")
    await add_member(owner, tenant_id=acme, user_id=member, role=Role.MEMBER)

    globex = await create_tenant(owner, name="Globex", slug=f"globex-{uuid.uuid4().hex[:8]}")
    globex_user = await create_user(owner, email="owner@globex.test", name="Gia")
    await add_member(owner, tenant_id=globex, user_id=globex_user, role=Role.OWNER)

    return {
        "acme": acme,
        "globex": globex,
        "acme_world": await make_conversation(owner, acme),
        "globex_world": await make_conversation(owner, globex),
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


async def _arm(db, world, clock):
    """Schedule step 1 for a tenant, as the AI pipeline would."""
    return await service(db, world["acme"], clock).on_reply_sent(
        world["acme_world"]["conversation_id"]
    )


async def test_listing_scheduled_tasks(db, api, world, clock):
    await _arm(db, world, clock)
    response = await api.get(TASKS, headers=world["owner_headers"])
    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    task = body["items"][0]
    assert task["task_type"] == "FOLLOW_UP"
    assert task["sequence_step"] == 1
    assert task["status"] == "PENDING"
    # Operational visibility: what is outstanding, at a glance.
    assert body["summary"]["PENDING"] == 1


async def test_fetching_one_task(db, api, world, clock):
    task = await _arm(db, world, clock)
    response = await api.get(f"{TASKS}/{task.id}", headers=world["owner_headers"])
    assert response.status_code == 200
    assert response.json()["id"] == str(task.id)


async def test_a_conversations_sequence_is_listed_in_step_order(db, api, world, clock):
    await _arm(db, world, clock)
    conversation_id = world["acme_world"]["conversation_id"]
    response = await api.get(
        f"{TASKS}/conversation/{conversation_id}", headers=world["owner_headers"]
    )
    assert response.status_code == 200
    assert response.json()["count"] == 1


async def test_a_conversation_with_no_tasks_is_a_404(db, api, world):
    """So 'no follow-ups' and 'not your conversation' are not reported identically."""
    response = await api.get(f"{TASKS}/conversation/{uuid.uuid4()}", headers=world["owner_headers"])
    assert response.status_code == 404


async def test_filtering_by_status(db, api, world, clock):
    await _arm(db, world, clock)
    pending = await api.get(f"{TASKS}?status=PENDING", headers=world["owner_headers"])
    completed = await api.get(f"{TASKS}?status=COMPLETED", headers=world["owner_headers"])
    assert pending.json()["count"] == 1
    assert completed.json()["count"] == 0


async def test_an_unknown_task_is_a_404(db, api, world):
    response = await api.get(f"{TASKS}/{uuid.uuid4()}", headers=world["owner_headers"])
    assert response.status_code == 404


async def test_an_anonymous_request_is_refused(db, api):
    assert (await api.get(TASKS)).status_code == 401


async def test_a_member_may_read_tasks(db, api, world, clock):
    await _arm(db, world, clock)
    response = await api.get(TASKS, headers=world["member_headers"])
    assert response.status_code == 200


async def test_the_response_never_exposes_internals(db, api, world, clock):
    """The worker identity is internal; the tenant id comes from the session, not the row."""
    await _arm(db, world, clock)
    response = await api.get(TASKS, headers=world["owner_headers"])
    item = response.json()["items"][0]
    for internal in ("tenant_id", "locked_by", "locked_at", "activity_anchor_at", "id_"):
        assert internal not in item


# -- the cross-tenant attack ------------------------------------------------


async def test_a_tenant_cannot_see_another_tenants_tasks(db, api, world, clock):
    task = await _arm(db, world, clock)
    headers = world["globex_headers"]
    conversation_id = world["acme_world"]["conversation_id"]

    listed = await api.get(TASKS, headers=headers)
    fetched = await api.get(f"{TASKS}/{task.id}", headers=headers)
    by_conversation = await api.get(f"{TASKS}/conversation/{conversation_id}", headers=headers)
    assert listed.json()["count"] == 0
    assert fetched.status_code == 404
    assert by_conversation.status_code == 404


async def test_a_non_member_of_the_target_tenant_is_refused(db, api, world):
    response = await api.get(
        TASKS, headers={**world["owner_headers"], "X-Tenant-ID": str(world["globex"])}
    )
    assert response.status_code == 403


async def test_the_surface_is_read_only(db, api, world, clock):
    """No mutation endpoint exists, so there is no second path to task state."""
    assert (await api.post(TASKS, json={})).status_code == 405
    task = await _arm(db, world, clock)
    deleted = await api.delete(f"{TASKS}/{task.id}", headers=world["owner_headers"])
    assert deleted.status_code == 405
