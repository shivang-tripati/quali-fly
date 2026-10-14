"""Phase 8 API: the handoff inbox, and the authorization on every transition.

Two things are being checked here that unit tests cannot reach. The first is the role
split: a MEMBER may claim, start and resolve, but only an ADMIN may put work on someone
else. The second is that the tenant never comes from the request - every call carries an
`X-Tenant-ID` header, and none of them can widen it.
"""

import uuid

import pytest

from app.core.clock import FixedClock
from app.models.human_handoff import HandoffReason, HandoffStatus
from app.models.membership import Role
from tests.factories import add_member, create_tenant, create_user, issue_session
from tests.follow_up_fixtures import make_conversation
from tests.handoff_fixtures import T0, ho_settings, make_handoff

HANDOFFS = "/api/v1/handoffs"


@pytest.fixture
async def world(owner):
    """Acme with an OWNER, an ADMIN and a MEMBER, plus a second tenant for attack tests."""
    acme = await create_tenant(owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    owner_user = await create_user(
        owner, email=f"owner-{uuid.uuid4().hex[:6]}@acme.test", name="Ada"
    )
    await add_member(owner, tenant_id=acme, user_id=owner_user, role=Role.OWNER)
    admin = await create_user(owner, email=f"admin-{uuid.uuid4().hex[:6]}@acme.test", name="Ana")
    await add_member(owner, tenant_id=acme, user_id=admin, role=Role.ADMIN)
    member = await create_user(owner, email=f"member-{uuid.uuid4().hex[:6]}@acme.test", name="Mo")
    await add_member(owner, tenant_id=acme, user_id=member, role=Role.MEMBER)
    outsider = await create_user(owner, email=f"out-{uuid.uuid4().hex[:6]}@other.test", name="Ol")

    globex = await create_tenant(owner, name="Globex", slug=f"globex-{uuid.uuid4().hex[:8]}")
    globex_user = await create_user(
        owner, email=f"owner-{uuid.uuid4().hex[:6]}@globex.test", name="Gia"
    )
    await add_member(owner, tenant_id=globex, user_id=globex_user, role=Role.OWNER)

    return {
        "acme": acme,
        "globex": globex,
        "acme_world": await make_conversation(owner, acme),
        "globex_world": await make_conversation(owner, globex),
        "owner_user": owner_user,
        "admin_user": admin,
        "member_user": member,
        "outsider_user": outsider,
        "owner_headers": {
            "X-Tenant-ID": str(acme),
            "Cookie": f"qf_session={await issue_session(owner, user_id=owner_user)}",
        },
        "admin_headers": {
            "X-Tenant-ID": str(acme),
            "Cookie": f"qf_session={await issue_session(owner, user_id=admin)}",
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


async def _open(db, world):
    """One OPEN handoff on Acme, and one on Globex for the isolation tests."""
    _, acme_creation, _ = await make_handoff(db, world["acme"], world["acme_world"])
    _, globex_creation, _ = await make_handoff(db, world["globex"], world["globex_world"])
    return acme_creation, globex_creation


# -- reads -------------------------------------------------------------------


async def test_listing_the_queue(db, api, world):
    creation, _ = await _open(db, world)

    response = await api.get(HANDOFFS, headers=world["owner_headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    item = body["items"][0]
    assert item["id"] == str(creation.handoff.id)
    assert item["status"] == "OPEN"
    assert item["reason"] == "HIGH_INTENT"
    assert item["lead_name"] == "Dana Buyer"
    assert item["latest_inbound_text"]
    assert body["summary"]["OPEN"] == 1
    assert body["summary"]["RESOLVED"] == 0


async def test_a_member_can_list_the_queue(db, api, world):
    await _open(db, world)
    response = await api.get(HANDOFFS, headers=world["member_headers"])
    assert response.status_code == 200
    assert response.json()["count"] == 1


async def test_fetching_one_handoff(db, api, world):
    creation, _ = await _open(db, world)
    response = await api.get(f"{HANDOFFS}/{creation.handoff.id}", headers=world["owner_headers"])
    assert response.status_code == 200
    assert response.json()["conversation_id"] == str(world["acme_world"]["conversation_id"])


async def test_another_tenants_handoff_is_a_404_not_a_403(db, api, world):
    """Not found rather than forbidden: existence is not another tenant's business."""
    _, globex_creation = await _open(db, world)

    response = await api.get(
        f"{HANDOFFS}/{globex_creation.handoff.id}", headers=world["owner_headers"]
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "HANDOFF_NOT_FOUND"


async def test_the_queue_can_be_filtered(db, api, world):
    creation, _ = await _open(db, world)

    by_status = await api.get(f"{HANDOFFS}?status=OPEN", headers=world["owner_headers"])
    by_reason = await api.get(f"{HANDOFFS}?reason=HIGH_INTENT", headers=world["owner_headers"])
    wrong_reason = await api.get(f"{HANDOFFS}?reason=AI_FAILURE", headers=world["owner_headers"])
    by_conversation = await api.get(
        f"{HANDOFFS}?conversation_id={world['acme_world']['conversation_id']}",
        headers=world["owner_headers"],
    )

    assert by_status.json()["count"] == 1
    assert by_reason.json()["count"] == 1
    assert wrong_reason.json()["count"] == 0
    assert by_conversation.json()["count"] == 1
    assert by_conversation.json()["items"][0]["id"] == str(creation.handoff.id)


async def test_listing_requires_authentication(db, api, world):
    assert (await api.get(HANDOFFS)).status_code == 401


async def test_the_queue_can_be_searched_by_lead_name(db, api, world):
    """What the command palette asks. Backslash-escaped, so `%` is a percent sign."""
    await _open(db, world)

    found = await api.get(f"{HANDOFFS}?search=Dana", headers=world["owner_headers"])
    nobody = await api.get(f"{HANDOFFS}?search=Nobody", headers=world["owner_headers"])
    wildcard = await api.get(f"{HANDOFFS}?search=%25", headers=world["owner_headers"])

    assert found.json()["count"] == 1
    assert nobody.json()["count"] == 0
    # A literal `%` matches no name, and must not degenerate into "match everything".
    assert wildcard.json()["count"] == 0


# -- the inbox ---------------------------------------------------------------


async def test_the_notification_inbox(db, api, world):
    creation, _ = await _open(db, world)

    response = await api.get(f"{HANDOFFS}/notifications/inbox", headers=world["owner_headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    note = body["items"][0]
    assert note["type"] == "HANDOFF_CREATED"
    assert note["handoff_id"] == str(creation.handoff.id)
    assert note["status"] == "UNREAD"
    assert note["payload"]["lead_name"] == "Dana Buyer"


async def test_another_tenants_inbox_is_empty(db, api, world):
    await _open(db, world)
    response = await api.get(f"{HANDOFFS}/notifications/inbox", headers=world["owner_headers"])
    assert response.json()["count"] == 1
    # Globex's own inbox has its own; Acme's list never mixes in.
    globex = await api.get(f"{HANDOFFS}/notifications/inbox", headers=world["globex_headers"])
    assert globex.json()["count"] == 1
    assert globex.json()["items"][0]["conversation_id"] == str(
        world["globex_world"]["conversation_id"]
    )


# -- mark read ----------------------------------------------------------------


async def test_opening_a_notification_marks_it_read(db, api, world):
    """The unread badge has to be able to go down, or people learn to ignore it."""
    await _open(db, world)
    before = (
        await api.get(f"{HANDOFFS}/notifications/inbox", headers=world["owner_headers"])
    ).json()
    assert before["items"][0]["status"] == "UNREAD"
    notification_id = before["items"][0]["id"]

    marked = await api.post(
        f"{HANDOFFS}/notifications/{notification_id}/read", headers=world["member_headers"]
    )
    assert marked.status_code == 204

    after = (
        await api.get(f"{HANDOFFS}/notifications/inbox", headers=world["owner_headers"])
    ).json()
    assert after["items"][0]["status"] == "READ"
    assert after["items"][0]["read_at"] is not None


async def test_marking_read_twice_is_still_a_success(db, api, world):
    """A double-clicked notification is what the person asked for, twice."""
    await _open(db, world)
    notification_id = (
        await api.get(f"{HANDOFFS}/notifications/inbox", headers=world["owner_headers"])
    ).json()["items"][0]["id"]

    first = await api.post(
        f"{HANDOFFS}/notifications/{notification_id}/read", headers=world["owner_headers"]
    )
    second = await api.post(
        f"{HANDOFFS}/notifications/{notification_id}/read", headers=world["owner_headers"]
    )

    assert first.status_code == second.status_code == 204


async def test_marking_another_tenants_notification_read_changes_nothing(db, api, world):
    """It answers 204 but must not reach across the boundary."""
    await _open(db, world)
    acme_note = (
        await api.get(f"{HANDOFFS}/notifications/inbox", headers=world["owner_headers"])
    ).json()["items"][0]

    response = await api.post(
        f"{HANDOFFS}/notifications/{acme_note['id']}/read", headers=world["globex_headers"]
    )
    assert response.status_code == 204

    still_unread = (
        await api.get(f"{HANDOFFS}/notifications/inbox", headers=world["owner_headers"])
    ).json()["items"][0]
    assert still_unread["status"] == "UNREAD"
    assert still_unread["read_at"] is None


async def test_marking_read_requires_authentication(db, api, world):
    await _open(db, world)
    notification_id = (
        await api.get(f"{HANDOFFS}/notifications/inbox", headers=world["owner_headers"])
    ).json()["items"][0]["id"]
    assert (await api.post(f"{HANDOFFS}/notifications/{notification_id}/read")).status_code == 401


# -- claim -------------------------------------------------------------------


async def test_claiming_a_handoff(db, api, world):
    creation, _ = await _open(db, world)

    response = await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/claim", headers=world["member_headers"]
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ASSIGNED"
    assert body["assigned_user_id"] == str(world["member_user"])
    assert body["assigned_at"] is not None


async def test_a_second_claim_conflicts(db, api, world):
    creation, _ = await _open(db, world)
    await api.post(f"{HANDOFFS}/{creation.handoff.id}/claim", headers=world["member_headers"])

    second = await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/claim", headers=world["admin_headers"]
    )

    assert second.status_code == 409
    assert second.json()["error"]["code"] == "HANDOFF_ALREADY_ASSIGNED"


async def test_claiming_something_that_does_not_exist(db, api, world):
    response = await api.post(f"{HANDOFFS}/{uuid.uuid4()}/claim", headers=world["owner_headers"])
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "HANDOFF_NOT_FOUND"


async def test_claiming_another_tenants_handoff_is_a_404(db, api, world):
    _, globex_creation = await _open(db, world)
    response = await api.post(
        f"{HANDOFFS}/{globex_creation.handoff.id}/claim", headers=world["owner_headers"]
    )
    assert response.status_code == 404


# -- assign ------------------------------------------------------------------


async def test_an_admin_can_assign_to_a_teammate(db, api, world):
    creation, _ = await _open(db, world)

    response = await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/assign",
        json={"user_id": str(world["member_user"])},
        headers=world["admin_headers"],
    )

    assert response.status_code == 200
    assert response.json()["assigned_user_id"] == str(world["member_user"])


async def test_a_member_cannot_assign_to_someone_else(db, api, world):
    """Claim yourself yes; put work on a colleague no."""
    creation, _ = await _open(db, world)

    response = await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/assign",
        json={"user_id": str(world["admin_user"])},
        headers=world["member_headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "ROLE_REQUIRED"


async def test_assigning_to_a_user_outside_the_tenant_is_refused(db, api, world):
    """`users` is global, so this id is real and simply belongs to someone else."""
    creation, _ = await _open(db, world)

    response = await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/assign",
        json={"user_id": str(world["outsider_user"])},
        headers=world["admin_headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "HANDOFF_ASSIGNMENT_NOT_ALLOWED"


async def test_an_admin_can_reassign_an_open_handoff(db, api, world):
    creation, _ = await _open(db, world)
    await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/assign",
        json={"user_id": str(world["member_user"])},
        headers=world["admin_headers"],
    )

    response = await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/assign",
        json={"user_id": str(world["admin_user"])},
        headers=world["admin_headers"],
    )

    assert response.status_code == 200
    assert response.json()["assigned_user_id"] == str(world["admin_user"])


async def test_assign_ignores_a_tenant_id_in_the_body(db, api, world):
    """The tenant comes from the session, so a smuggled field changes nothing."""
    creation, _ = await _open(db, world)

    response = await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/assign",
        json={"user_id": str(world["member_user"]), "tenant_id": str(world["globex"])},
        headers=world["admin_headers"],
    )

    assert response.status_code == 200
    # And the result is still Acme's handoff.
    reread = await api.get(f"{HANDOFFS}/{creation.handoff.id}", headers=world["owner_headers"])
    assert reread.json()["assigned_user_id"] == str(world["member_user"])


# -- start / resolve ---------------------------------------------------------


async def test_starting_a_claimed_handoff(db, api, world):
    creation, _ = await _open(db, world)
    await api.post(f"{HANDOFFS}/{creation.handoff.id}/claim", headers=world["member_headers"])

    response = await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/start", headers=world["member_headers"]
    )

    assert response.status_code == 200
    assert response.json()["status"] == "IN_PROGRESS"


async def test_resolving_a_handoff_with_a_note(db, api, world):
    creation, _ = await _open(db, world)

    response = await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/resolve",
        json={"note": "Called and quoted; deal in progress."},
        headers=world["member_headers"],
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "RESOLVED"
    assert body["resolved_at"] is not None
    assert body["resolution_note"] == "Called and quoted; deal in progress."


async def test_resolving_twice_is_refused(db, api, world):
    creation, _ = await _open(db, world)
    await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/resolve", json={}, headers=world["owner_headers"]
    )

    again = await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/resolve", json={}, headers=world["owner_headers"]
    )

    assert again.status_code == 409
    assert again.json()["error"]["code"] == "HANDOFF_RESOLUTION_NOT_ALLOWED"


async def test_a_resolved_handoff_cannot_be_claimed(db, api, world):
    creation, _ = await _open(db, world)
    await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/resolve", json={}, headers=world["owner_headers"]
    )

    response = await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/claim", headers=world["member_headers"]
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "HANDOFF_INVALID_STATE"


async def test_cross_tenant_resolve_is_a_404_and_changes_nothing(db, api, world):
    _, globex_creation = await _open(db, world)

    response = await api.post(
        f"{HANDOFFS}/{globex_creation.handoff.id}/resolve",
        json={"note": "nope"},
        headers=world["owner_headers"],
    )

    assert response.status_code == 404
    reread = await api.get(
        f"{HANDOFFS}/{globex_creation.handoff.id}", headers=world["globex_headers"]
    )
    assert reread.json()["status"] == "OPEN"
    assert reread.json()["resolution_note"] is None


# -- the state machine through the API ---------------------------------------


async def test_the_full_workflow_through_the_api(db, api, world):
    """OPEN -> ASSIGNED -> IN_PROGRESS -> RESOLVED, in order, over HTTP."""
    creation, _ = await _open(db, world)
    handoff_id = creation.handoff.id

    claimed = await api.post(f"{HANDOFFS}/{handoff_id}/claim", headers=world["member_headers"])
    started = await api.post(f"{HANDOFFS}/{handoff_id}/start", headers=world["member_headers"])
    resolved = await api.post(
        f"{HANDOFFS}/{handoff_id}/resolve",
        json={"note": "handled"},
        headers=world["member_headers"],
    )

    assert [claimed.json()["status"], started.json()["status"], resolved.json()["status"]] == [
        "ASSIGNED",
        "IN_PROGRESS",
        "RESOLVED",
    ]


async def test_resolving_does_not_rearm_the_follow_up_sequence(db, api, world):
    """A resolved handoff is history; the conversation does not go back to automation."""
    from app.db.tenant_context import set_current_tenant
    from app.repositories import scheduled_tasks as task_repo
    from app.services.follow_ups import FollowUpService
    from tests.fakes import FakeGraph

    world_ = world["acme_world"]
    await FollowUpService(
        db,
        world["acme"],
        settings=ho_settings(),
        clock=FixedClock(T0),
        graph=FakeGraph(),
    ).on_reply_sent(world_["conversation_id"])
    _, creation, _ = await make_handoff(db, world["acme"], world_)

    await api.post(
        f"{HANDOFFS}/{creation.handoff.id}/resolve", json={}, headers=world["owner_headers"]
    )

    async with db() as session, session.begin():
        await set_current_tenant(session, world["acme"])
        tasks = await task_repo.get_by_conversation(
            session, tenant_id=world["acme"], conversation_id=world_["conversation_id"]
        )
    assert [t.status.value for t in tasks] == ["CANCELLED"], "the sequence stays cancelled"
    assert HandoffStatus.RESOLVED.value == "RESOLVED"


async def test_the_queue_sorts_by_priority_then_age(db, api, world, owner):
    """An explicit request outranks a provider failure, in the list an agent reads.

    Three handoffs, created in an order that does *not* match priority, so a listing sorted
    by creation time would fail this.
    """
    from app.services.handoffs import HumanHandoffService

    service = HumanHandoffService(db, world["acme"], settings=ho_settings(), clock=FixedClock(T0))
    conversations = [await make_conversation(owner, world["acme"]) for _ in range(3)]
    for conv, reason in zip(
        conversations,
        (HandoffReason.AI_FAILURE, HandoffReason.OTHER, HandoffReason.EXPLICIT_HUMAN_REQUEST),
        strict=True,
    ):
        await service.create_or_get_active_handoff(conv["conversation_id"], reason=reason)

    response = await api.get(HANDOFFS, headers=world["owner_headers"])

    assert response.status_code == 200
    reasons = [item["reason"] for item in response.json()["items"]]
    assert reasons == [
        "EXPLICIT_HUMAN_REQUEST",  # 300
        "OTHER",  # 100
        "AI_FAILURE",  # 50
    ]
    assert response.json()["summary"]["OPEN"] == 3
