"""The dashboard read model and endpoint.

Two things are being tested, and they are not the same thing:

* **Does it answer the question?** An attention item must be work a human could do right
  now. The negative cases matter as much as the positive ones - a dashboard that lists
  everything is a dashboard nobody reads, and the test that proves it is worth as much as
  the one that proves it finds the real problems.
* **Does it leak?** Every number is tenant scoped. A dashboard is the first screen after
  sign-in and the most damaging place for a cross-tenant read to surface.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import Depends

from app.db.tenant_context import set_current_tenant
from app.models.conversation import Channel, Message, MessageDelivery, MessageDirection
from app.models.membership import Role
from app.repositories import scheduled_tasks as task_repo
from app.services.handoffs import HumanHandoffService
from tests.factories import create_tenant, issue_session
from tests.fakes import FakeGraph
from tests.follow_up_fixtures import make_conversation
from tests.handoff_fixtures import T0, ho_settings, make_handoff, make_user

DASHBOARD = "/api/v1/dashboard"


async def _world(owner, db, *, role: Role = Role.OWNER):
    """A tenant with a user, one conversation, and a lead."""
    from app.core.clock import FixedClock

    tenant = await create_tenant(owner, name="Acme", slug=f"acme-{uuid.uuid4().hex[:8]}")
    user = await make_user(owner, role=role, tenant_id=tenant)
    world = await make_conversation(owner, tenant)
    headers = {
        "X-Tenant-ID": str(tenant),
        "Cookie": f"qf_session={await issue_session(owner, user_id=user.id)}",
    }
    return tenant, user, world, headers, FixedClock(T0)


def _pin_clock(app, clock):
    """Override the router's service dependency so `now` is the test's clock.

    "Overdue" and "due soon" are functions of time. Arranging rows around whatever the wall
    clock says today would make these tests pass today and drift tomorrow; pinning the
    clock keeps them deterministic without changing production code for the test's benefit.

    The override repeats the original's parameter annotations deliberately: FastAPI derives
    the sub-dependency tree from the *override's* signature, so an unannotated `session,
    context` would be read as two required query parameters and every request 422s.
    """
    from app.api.dashboard import dashboard_service
    from app.api.deps import CurrentUserSession, require_minimum_role
    from app.core.auth import TenantContext
    from app.models.membership import Role
    from app.services.dashboard import DashboardService

    ctx = Annotated[TenantContext, Depends(require_minimum_role(Role.MEMBER))]

    async def pinned(session: CurrentUserSession, context: ctx) -> DashboardService:
        return DashboardService(
            session,
            context.tenant_id,
            role=context.role,
            user_id=context.user_id,
            clock=clock,
        )

    app.dependency_overrides[dashboard_service] = pinned


async def _get(app, api, headers, *, clock=None):
    """Fetch the dashboard, optionally with a pinned clock."""
    if clock is not None:
        _pin_clock(app, clock)
    response = await api.get(DASHBOARD, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()


# -- shape -------------------------------------------------------------------


async def test_the_dashboard_renders_for_an_empty_tenant(db, api, app, owner):
    """An empty workspace is a real state, and it must not error.

    Every count zero, no attention items, and the admin integration block present but empty.
    """
    tenant, _, _, headers, _ = await _world(owner, db)

    body = await _get(app, api, headers)

    assert body["summary"] == {
        "new_leads": 1,
        # The fixture's single lead was created just now, so it is both an unanswered lead
        # and an arrival today. Asserting both separately is the point: they are different
        # numbers and a dashboard that conflates them reports the wrong one.
        "new_leads_today": 1,
        "qualified_leads": 0,
        "handoffs_open": 0,
        "handoffs_unassigned": 0,
        "handoffs_mine": 0,
        "follow_ups_due": 0,
        "follow_ups_overdue": 0,
        "follow_ups_upcoming": 0,
        "knowledge_documents": 0,
    }
    # The one lead is NEW, and the workspace has no knowledge - which is actionable.
    assert body["lead_activity"]["new"] == 1
    kinds = [item["kind"] for item in body["attention"]]
    assert "knowledge" in kinds
    assert "handoff" not in kinds


async def test_integration_is_admin_only(db, api, app, owner):
    """A MEMBER does not own the workspace connection, so it is not shown to them."""
    _, _, _, member_headers, _ = await _world(owner, db, role=Role.MEMBER)
    member = await _get(app, api, member_headers)
    assert member["integration"] is None

    _, _, _, admin_headers, _ = await _world(owner, db, role=Role.ADMIN)
    admin = await _get(app, api, admin_headers)
    assert admin["integration"] is not None
    assert admin["integration"]["meta_connection_count"] == 1
    assert "access_token" not in str(admin), "never any credential material"


async def test_unanswered_leads_and_leads_that_arrived_today_are_different_numbers(
    db, api, app, owner
):
    """The regression: the dashboard said "12 new leads today" using a lifecycle count.

    An unanswered lead from last week is still `NEW`, and it is emphatically not an arrival
    from today. The greeting on the frontend describes the second number, so conflating them
    makes the first line of the product wrong in a way nobody would notice until it was
    embarrassing in front of a customer.
    """
    from sqlalchemy import update

    from app.models.lead import Lead

    tenant, _, _, headers, clock = await _world(owner, db)
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant)
        await session.execute(
            update(Lead)
            .where(Lead.tenant_id == tenant)
            .values(created_at=clock.now() - timedelta(days=3))
        )

    body = await _get(app, api, headers, clock=clock)

    assert body["summary"]["new_leads"] == 1, "still unanswered"
    assert body["summary"]["new_leads_today"] == 0, "but did not arrive today"


# -- the attention queue -----------------------------------------------------


async def test_an_unclaimed_handoff_is_the_first_thing_shown(db, api, app, owner):
    tenant, _, world, headers, _ = await _world(owner, db)
    await make_handoff(db, tenant, world, graph=FakeGraph())

    body = await _get(app, api, headers)

    item = next(i for i in body["attention"] if i["kind"] == "handoff")
    assert item["severity"] == "critical", "an escalation nobody has taken is the top priority"
    assert item["label"] == "UNCLAIMED"
    assert item["title"] == "Dana Buyer"
    assert "high" in item["detail"].lower() or "ready to buy" in item["detail"]
    assert item["handoff_id"] is not None
    assert item["conversation_id"] == str(world["conversation_id"])
    assert item["waiting_seconds"] >= 0
    assert body["summary"]["handoffs_open"] == 1
    assert body["summary"]["handoffs_unassigned"] == 1


async def test_a_claimed_handoff_drops_to_informational(db, api, app, owner):
    """Once someone owns it, it is no longer interrupting - it is just visible."""
    tenant, agent, world, headers, _ = await _world(owner, db)
    _, creation, _ = await make_handoff(db, tenant, world, graph=FakeGraph())
    await HumanHandoffService(db, tenant, settings=ho_settings(), clock=_clock()).claim(
        creation.handoff.id, agent.id
    )

    body = await _get(app, api, headers)

    item = next(i for i in body["attention"] if i["kind"] == "handoff")
    assert item["severity"] == "info"
    # Claimed, not started. `claim` puts it in ASSIGNED; "IN PROGRESS" here would tell an
    # operator the work had begun when nobody had opened it yet.
    assert item["label"] == "CLAIMED"
    assert body["summary"]["handoffs_unassigned"] == 0
    assert body["summary"]["handoffs_mine"] == 1


async def test_only_a_started_handoff_reads_as_in_progress(db, api, app, owner):
    """The label tracks the real state, not a guess about intent.

    This is the distinction that was previously wrong: ASSIGNED and IN_PROGRESS both mean
    "someone owns it", so severity is the same for both, but only one of them means anybody
    has actually started work.
    """
    tenant, agent, world, headers, _ = await _world(owner, db)
    _, creation, _ = await make_handoff(db, tenant, world, graph=FakeGraph())
    service = HumanHandoffService(db, tenant, settings=ho_settings(), clock=_clock())
    await service.claim(creation.handoff.id, agent.id)
    await service.start(creation.handoff.id)

    body = await _get(app, api, headers)

    item = next(i for i in body["attention"] if i["kind"] == "handoff")
    assert item["label"] == "IN PROGRESS"
    assert item["severity"] == "info"


async def test_an_ordinary_new_lead_is_not_attention(db, api, app, owner):
    """The negative case, and the one that keeps the list short.

    Nobody is waiting on a specific person to do something about one unworked lead. Listing
    it would train the user to ignore the queue.
    """
    _, _, _, headers, _ = await _world(owner, db)
    body = await _get(app, api, headers)
    assert [i for i in body["attention"] if i["kind"] == "handoff"] == []


async def test_an_overdue_follow_up_is_attention_and_a_future_one_is_not(db, api, app, owner):
    from app.services.follow_ups import FollowUpService

    tenant, _, world, headers, clock = await _world(owner, db)
    # Arm, then advance well past due so the task is overdue.
    await FollowUpService(
        db, tenant, settings=ho_settings(), clock=clock, graph=FakeGraph()
    ).on_reply_sent(world["conversation_id"])
    clock.advance(hours=5)

    body = await _get(app, api, headers, clock=clock)

    item = next(i for i in body["attention"] if i["kind"] == "follow_up_overdue")
    assert item["severity"] == "warning"
    assert item["label"] == "OVERDUE"
    assert body["summary"]["follow_ups_overdue"] == 1


async def test_a_due_soon_follow_up_is_informational(db, api, app, owner):
    from app.services.follow_ups import FollowUpService

    tenant, _, world, headers, clock = await _world(owner, db)
    await FollowUpService(
        db, tenant, settings=ho_settings(), clock=clock, graph=FakeGraph()
    ).on_reply_sent(world["conversation_id"])
    # 4h due, clock still at T0: due in 4 hours, so "soon" rather than "overdue".
    body = await _get(app, api, headers, clock=clock)
    kinds = [i["kind"] for i in body["attention"]]
    assert "follow_up_due" in kinds
    assert "follow_up_overdue" not in kinds


async def test_a_ghost_task_is_never_surfaced_as_work(db, api, app, owner):
    """Scheduler vocabulary must not leak into the product.

    A ghost task is internal bookkeeping for a lead who went quiet. Rendering "1 follow-up
    due" for someone nobody has heard from in a week is exactly the confusion the product
    vocabulary exists to prevent.
    """
    from app.models.scheduled_task import TaskType

    tenant, _, world, headers, _ = await _world(owner, db)
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant)
        await task_repo.schedule(
            session,
            tenant_id=tenant,
            conversation_id=world["conversation_id"],
            lead_id=world["lead_id"],
            task_type=TaskType.GHOST,
            sequence_step=1,
            scheduled_at=T0,
            max_attempts=3,
            activity_anchor_at=T0,
        )

    body = await _get(app, api, headers)

    assert [i for i in body["attention"] if i["kind"].startswith("follow_up")] == []
    assert body["summary"]["follow_ups_due"] == 0


async def test_a_failed_ai_run_is_attention_with_a_category_not_provider_text(db, api, app, owner):
    from app.models.ai_message_run import AIMessageRun, AIRunStatus

    tenant, _, world, headers, _ = await _world(owner, db)
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant)
        run = AIMessageRun(
            tenant_id=tenant,
            conversation_id=world["conversation_id"],
            inbound_message_id=world["inbound_message_id"],
        )
        run.status = AIRunStatus.FAILED
        run.error_category = "transient"
        run.completed_at = datetime.now(UTC)
        session.add(run)

    body = await _get(app, api, headers)

    item = next(i for i in body["attention"] if i["kind"] == "ai_failure")
    assert item["label"] == "TRANSIENT"
    assert item["title"] == "Dana Buyer", "an operator needs to know whose reply failed"
    assert "provider" not in item["detail"].lower()


async def test_an_old_failure_is_no_longer_attention(db, api, app, owner):
    """A three-day-old failure has been seen or is not worth interrupting for."""
    from app.models.ai_message_run import AIMessageRun, AIRunStatus

    tenant, _, world, headers, _ = await _world(owner, db)
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant)
        run = AIMessageRun(
            tenant_id=tenant,
            conversation_id=world["conversation_id"],
            inbound_message_id=world["inbound_message_id"],
        )
        run.status = AIRunStatus.FAILED
        run.error_category = "transient"
        run.completed_at = datetime.now(UTC) - timedelta(days=3)
        session.add(run)

    body = await _get(app, api, headers)
    assert [i for i in body["attention"] if i["kind"] == "ai_failure"] == []


# -- ordering and payload ----------------------------------------------------


async def test_attention_is_ordered_by_severity_then_age(db, api, app, owner):
    tenant, _, world, headers, _ = await _world(owner, db)
    await make_handoff(db, tenant, world, graph=FakeGraph())
    # Two tenants' worth of noise so ordering is actually exercised.
    other = await make_conversation(owner, tenant)
    await make_handoff(db, tenant, other, graph=FakeGraph())

    body = await _get(app, api, headers)

    severities = [i["severity"] for i in body["attention"]]
    order = {"critical": 0, "warning": 1, "info": 2}
    ranks = [order[s] for s in severities]
    assert ranks == sorted(ranks), "severity must be the primary sort"
    criticals = [i for i in body["attention"] if i["severity"] == "critical"]
    waits = [i["waiting_seconds"] for i in criticals]
    assert waits == sorted(waits, reverse=True), "oldest first within a severity"


async def test_recent_conversations_carry_what_the_inbox_needs(db, api, app, owner):
    tenant, _, world, headers, clock = await _world(owner, db)
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant)
        session.add(
            Message(
                tenant_id=tenant,
                conversation_id=world["conversation_id"],
                channel=Channel.MESSENGER,
                direction=MessageDirection.INBOUND,
                delivery=MessageDelivery.SENT,
                meta_message_id=f"mid.{uuid.uuid4().hex[:10]}",
                message_type="text",
                text="Is the van still available?",
                sent_at=datetime.now(UTC),
            )
        )

    # The clock is pinned deliberately. `make_conversation` stamps `last_inbound_at` from
    # `follow_up_fixtures.T0` - 2026-09-28 10:00, a day before `handoff_fixtures.T0` - so
    # whether this conversation is inside the 24-hour window depends on the wall clock. Left
    # unpinned it passed only until 2026-09-29 10:00 UTC and then failed for a day with no
    # code change, which is a test with an expiry rather than a product bug: the dashboard
    # answered `False` correctly because the message really was more than 24 hours old.
    body = await _get(app, api, headers, clock=clock)

    item = next(c for c in body["recent_conversations"] if c["id"] == str(world["conversation_id"]))
    assert item["last_message_text"] == "Is the van still available?"
    assert item["last_message_direction"] == "INBOUND"
    assert item["lead_name"] == "Dana Buyer"
    assert item["within_messaging_window"] is True
    assert item["has_active_handoff"] is False


async def test_the_response_never_contains_scheduler_internals(db, api, app, owner):
    """Scheduler and provider internals must not reach the product's language.

    `HUMAN_HANDOFF` is deliberately *not* on this list: it is the PRD's own lead lifecycle
    vocabulary (Sprint 3.4) and the UI needs it to render the right badge. What must never
    appear is the machinery - a task type, a step number, a lease, an attempt counter.
    """
    tenant, _, world, headers, _ = await _world(owner, db)
    await make_handoff(db, tenant, world, graph=FakeGraph())

    blob = str(await _get(app, api, headers))

    for forbidden in (
        "task_type",
        "sequence_step",
        "locked_by",
        "locked_at",
        "max_attempts",
        "attempt_count",
        "FOLLOW_UP",
        "GHOST",
        "PENDING",
        "PROCESSING",
    ):
        assert forbidden not in blob, f"{forbidden} is a database value, not product language"


# -- isolation ---------------------------------------------------------------


async def test_the_dashboard_is_tenant_scoped(db, api, app, owner):
    """A tenant's dashboard must contain nothing from another tenant."""
    acme, _, acme_world, acme_headers, _ = await _world(owner, db)
    globex, _, globex_world, globex_headers, _ = await _world(owner, db)
    await make_handoff(db, acme, acme_world, graph=FakeGraph())
    await make_handoff(db, globex, globex_world, graph=FakeGraph())

    acme_body = await _get(app, api, acme_headers)
    globex_body = await _get(app, api, globex_headers)

    acme_ids = {i["handoff_id"] for i in acme_body["attention"] if i["handoff_id"]}
    globex_ids = {i["handoff_id"] for i in globex_body["attention"] if i["handoff_id"]}
    assert acme_ids and globex_ids
    assert not (acme_ids & globex_ids), "handoff ids crossed a tenant boundary"
    assert str(globex_world["conversation_id"]) not in str(acme_body)
    assert str(acme_world["conversation_id"]) not in str(globex_body)


async def test_the_dashboard_requires_a_session(db, api, app, owner):
    assert (await api.get(DASHBOARD)).status_code == 401


# -- helpers -----------------------------------------------------------------


def _clock():
    from app.core.clock import FixedClock

    return FixedClock(T0)
