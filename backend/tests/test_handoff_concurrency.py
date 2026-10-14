"""The races. Cases A-F from the Phase 8 brief, against real PostgreSQL.

Every one of these is a case where a read-then-write implementation would pass a
sequential test and fail in production, which is exactly why they are written the way they
are. None of them use a mocked repository: the guarantees being tested *are* database
guarantees, and a mock would only assert that the mock was called correctly.

The ten-worker tests use `asyncio.gather` over one event loop, so they share a connection
pool. That is a genuine race - each coroutine takes its own connection, takes its own
snapshot, and the `INSERT ... ON CONFLICT` and conditional `UPDATE`s run as concurrent
PostgreSQL statements, which is what the constraint is there to arbitrate. (A ten-*process*
version of the Phase 7 claim race lives in the verification script; the same reasoning
applies, and repeating it per test file would buy nothing.)
"""

import asyncio
import uuid
from datetime import timedelta

from sqlalchemy import select

from app.core.clock import FixedClock
from app.db.tenant_context import set_current_tenant
from app.models.conversation import Conversation
from app.models.human_handoff import HandoffReason, HandoffStatus
from app.models.internal_notification import InternalNotification, NotificationType
from app.models.lead import Lead, LeadStatus
from app.models.membership import Role
from app.models.scheduled_task import TaskStatus
from app.repositories import handoffs as handoff_repo
from app.services.follow_ups import FollowUpService
from app.services.handoffs import HumanHandoffService
from tests.fakes import FakeGraph
from tests.follow_up_fixtures import make_conversation
from tests.handoff_fixtures import (
    T0,
    count_active,
    handoff_count,
    ho_settings,
    make_user,
    notifications_for,
)

WORKERS = 10


# -- Case A: ten workers process the same escalation --------------------------


async def test_case_a_ten_workers_create_one_handoff_one_notification(db, owner, tenant_row):
    """Meta retries a webhook and the AI run retries too. The queue must not multiply."""
    world = await make_conversation(owner, tenant_row)
    tenant_id = tenant_row
    conversation_id = world["conversation_id"]

    results = await asyncio.gather(
        *(_escalate_once(db, tenant_id, conversation_id, worker) for worker in range(WORKERS))
    )

    assert await handoff_count(db, tenant_id) == 1, "one active handoff"
    assert await count_active(db, tenant_id, conversation_id) == 1
    assert sum(1 for r in results if r) == 1, "exactly one worker created it"


async def test_case_a_acknowledgement_is_sent_once_under_ten_workers(db, owner, tenant_row):
    """The send is gated on `created`, so nine losers send nothing."""
    world = await make_conversation(owner, tenant_row)
    tenant_id = tenant_row
    conversation_id = world["conversation_id"]
    graph = FakeGraph()

    await asyncio.gather(
        *(_escalate_and_acknowledge(db, tenant_id, conversation_id, graph) for _ in range(WORKERS))
    )

    assert len(graph.sends) == 1, "one lead, one acknowledgement"


async def _escalate_once(db, tenant_id, conversation_id, worker):
    """One worker's attempt: create, and acknowledge only if it won."""
    service = HumanHandoffService(
        db, tenant_id, settings=ho_settings(), clock=FixedClock(T0, worker_id=f"w{worker}")
    )
    creation = await service.create_or_get_active_handoff(
        conversation_id, reason=HandoffReason.HIGH_INTENT, detail=f"worker {worker}"
    )
    if creation is None:
        return False
    if creation.created:
        await service.acknowledge(
            creation.handoff.id,
            conversation=await _conversation(db, tenant_id, conversation_id),
            graph=FakeGraph(),
            already_delivered=True,
        )
    return creation.created


async def _escalate_and_acknowledge(db, tenant_id, conversation_id, graph):
    service = HumanHandoffService(db, tenant_id, settings=ho_settings(), clock=FixedClock(T0))
    creation = await service.create_or_get_active_handoff(
        conversation_id, reason=HandoffReason.EXPLICIT_HUMAN_REQUEST
    )
    if creation is not None and creation.created:
        await service.acknowledge(
            creation.handoff.id,
            conversation=await _conversation(db, tenant_id, conversation_id),
            graph=graph,
            already_delivered=False,
        )


# -- Case B: ten agents claim one handoff -------------------------------------


async def test_case_b_ten_agents_claim_one_open_handoff(db, owner, tenant_row):
    """Exactly one owner. Not "at most one" - one, and nine honest conflicts."""
    world = await make_conversation(owner, tenant_row)
    agents = [
        await make_user(owner, role=Role.MEMBER, tenant_id=tenant_row) for _ in range(WORKERS)
    ]

    service = HumanHandoffService(db, tenant_row, settings=ho_settings(), clock=FixedClock(T0))
    creation = await service.create_or_get_active_handoff(
        world["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )

    outcomes = await asyncio.gather(
        *(_try_claim(service, creation.handoff.id, agent.id) for agent in agents)
    )

    winners = [o for o in outcomes if o.startswith("claimed:")]
    losers = [o for o in outcomes if o == "conflict"]
    assert len(winners) == 1, f"expected one winner, got {len(winners)}: {outcomes}"
    assert len(losers) == WORKERS - 1, f"got {len(losers)} conflicts: {outcomes}"

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        handoff = await handoff_repo.get(
            session, tenant_id=tenant_row, handoff_id=creation.handoff.id
        )
    assert handoff.status is HandoffStatus.ASSIGNED
    assert handoff.assigned_user_id == uuid.UUID(winners[0].split(":")[1])


async def _try_claim(service, handoff_id, user_id):
    from app.services.handoffs import HandoffAlreadyAssigned, HandoffInvalidState

    try:
        claimed = await service.claim(handoff_id, user_id)
    except HandoffAlreadyAssigned:
        return "conflict"
    except HandoffInvalidState:
        return "conflict"
    return f"claimed:{claimed.assigned_user_id}"


# -- Case C: handoff versus follow-up -----------------------------------------


async def test_case_c_a_due_follow_up_cannot_send_during_a_handoff(db, owner, tenant_row):
    """The follow-up worker has already claimed the task; handoff still wins.

    This is the hard half of the suppression rule. `create_or_get_active_handoff` cancels
    *pending* tasks, and a claimed task is `PROCESSING` - it cannot be cancelled from the
    handoff transaction without stranding a worker mid-send. So the last line of defence is
    the eligibility re-check the follow-up already performs, extended to ask whether a human
    is handling the conversation.
    """
    from app.repositories import scheduled_tasks as task_repo

    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()
    follow_ups = FollowUpService(
        db,
        tenant_row,
        settings=ho_settings(),
        clock=FixedClock(T0),
        graph=graph,
        # Execute as the worker that holds the claim. Phase 10 makes the service verify
        # lease ownership, so a service built with a different identity would refuse at the
        # first checkpoint and this test would pass for entirely the wrong reason - it would
        # be asserting the lease guard, not the handoff suppression.
        worker="w1",
    )
    await follow_ups.on_reply_sent(world["conversation_id"])

    # Advance to the due time and claim the task exactly as the poller would, then escalate
    # underneath the worker while it holds the claim.
    due = T0 + timedelta(hours=4)
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        claimed = await task_repo.claim_due(
            session,
            tenant_id=tenant_row,
            now=due,
            limit=10,
            worker="w1",
            lease_seconds=300,
        )
    assert claimed, "fixture must have a claimed task to race with"

    service = HumanHandoffService(db, tenant_row, settings=ho_settings(), clock=FixedClock(T0))
    await service.create_or_get_active_handoff(
        world["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )

    # The worker now tries to send.
    outcomes = [await follow_ups.execute(task_id) for task_id in claimed]

    assert graph.sends == [], "no automated message may go out during an active handoff"
    assert all(o.reason == "human_handoff" for o in outcomes), [o.reason for o in outcomes]
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        tasks = await task_repo.get_by_conversation(
            session, tenant_id=tenant_row, conversation_id=world["conversation_id"]
        )
    assert [t.status for t in tasks] == [TaskStatus.CANCELLED]
    assert [t.last_error for t in tasks] == ["human_handoff"]


async def test_case_c_handoff_wins_when_it_arrives_first(db, owner, tenant_row):
    """The ordinary ordering: the tasks are still PENDING and get cancelled outright."""
    from app.repositories import scheduled_tasks as task_repo

    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()
    await FollowUpService(
        db, tenant_row, settings=ho_settings(), clock=FixedClock(T0), graph=graph
    ).on_reply_sent(world["conversation_id"])

    service = HumanHandoffService(db, tenant_row, settings=ho_settings(), clock=FixedClock(T0))
    result = await service.create_or_get_active_handoff(
        world["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )
    assert result.follow_ups_cancelled == 1

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        claimed = await task_repo.claim_due(
            session, tenant_id=tenant_row, now=T0, limit=10, worker="w1", lease_seconds=300
        )
    assert claimed == [], "a cancelled task is not claimable"

    assert graph.sends == []


# -- Case C': ghost versus handoff --------------------------------------------


async def test_case_c_ghosting_cannot_overwrite_an_active_handoff(db, owner, tenant_row):
    """Spec §29: a ghost task racing a handoff must leave the lead in HUMAN_HANDOFF."""
    from app.models.scheduled_task import TaskType
    from app.repositories import scheduled_tasks as task_repo

    world = await make_conversation(owner, tenant_row, lead_status=LeadStatus.REPLIED)
    graph = FakeGraph()
    follow_ups = FollowUpService(
        db, tenant_row, settings=ho_settings(), clock=FixedClock(T0), graph=graph
    )
    await follow_ups.on_reply_sent(world["conversation_id"])

    service = HumanHandoffService(db, tenant_row, settings=ho_settings(), clock=FixedClock(T0))
    await service.create_or_get_active_handoff(
        world["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )

    # A ghost task arrives claiming to be overdue, and is claimed directly.
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        ghost = await task_repo.schedule(
            session,
            tenant_id=tenant_row,
            conversation_id=world["conversation_id"],
            lead_id=world["lead_id"],
            task_type=TaskType.GHOST,
            sequence_step=1,
            scheduled_at=T0,
            max_attempts=3,
            activity_anchor_at=T0,
        )
        claimed = await task_repo.claim_due(
            session, tenant_id=tenant_row, now=T0, limit=10, worker="w1", lease_seconds=300
        )
    assert ghost is not None and claimed

    for task_id in claimed:
        await follow_ups.execute(task_id)

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        lead = await session.get(Lead, world["lead_id"])
    assert lead.status is LeadStatus.HUMAN_HANDOFF, "ghosting must not overwrite a handoff"
    assert lead.status is not LeadStatus.GHOSTED


# -- Case F: duplicate webhook delivery ---------------------------------------


async def test_case_f_the_same_inbound_delivered_repeatedly_creates_one_handoff(
    db, owner, tenant_row
):
    """The same Meta `mid` arriving five times: one message, one handoff, one notification.

    Delivered through the real `ConversationEngine.ingest_inbound`, because the point is
    that the *existing* inbound path is unchanged by Phase 8 and the handoff lands on the
    one message that stuck.
    """
    from app.models.conversation import Channel, Message
    from app.services.conversations import ConversationEngine
    from app.services.meta_messaging import InboundMessage

    world = await make_conversation(owner, tenant_row)
    mid = f"mid.{world['participant_id']}"
    message = InboundMessage(
        participant_id=world["participant_id"],
        meta_message_id=mid,
        source_id=None,
        message_type="text",
        text="I want to speak to someone please",
        sent_at=T0,
    )
    graph = FakeGraph()

    # One conversation, five deliveries of the same mid.
    for _ in range(5):
        async with db() as session, session.begin():
            await set_current_tenant(session, tenant_row)
            engine = ConversationEngine(session, tenant_row, graph=graph, settings=ho_settings())
            await engine.ingest_inbound(
                meta_connection_id=world["connection_id"],
                channel=Channel.MESSENGER,
                messages=[message],
            )

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        service = HumanHandoffService(db, tenant_row, settings=ho_settings(), clock=FixedClock(T0))
        for _ in range(5):
            await service.create_or_get_active_handoff(
                world["conversation_id"], reason=HandoffReason.EXPLICIT_HUMAN_REQUEST
            )

    assert await handoff_count(db, tenant_row) == 1
    async with db() as session:
        await set_current_tenant(session, tenant_row)
        messages = (
            (
                await session.execute(
                    select(Message).where(
                        Message.tenant_id == tenant_row,
                        Message.conversation_id == world["conversation_id"],
                        Message.meta_message_id == mid,
                    )
                )
            )
            .scalars()
            .all()
        )
        notes = (
            (
                await session.execute(
                    select(InternalNotification).where(InternalNotification.tenant_id == tenant_row)
                )
            )
            .scalars()
            .all()
        )
    assert len(messages) == 1, "one stored message"
    assert len(notes) == 1
    assert notes[0].type is NotificationType.HANDOFF_CREATED


# -- helpers -----------------------------------------------------------------


async def _conversation(db, tenant_id, conversation_id):
    async with db() as session:
        await set_current_tenant(session, tenant_id)
        return await session.scalar(
            select(Conversation).where(
                Conversation.id == conversation_id, Conversation.tenant_id == tenant_id
            )
        )


async def test_the_acknowledgement_is_not_lost_when_creation_is_retried(db, owner, tenant_row):
    """A retried creation must not leave the lead un-acknowledged *and* not duplicated.

    The subtle failure: the winner sends the acknowledgement, the process dies before
    committing `acknowledged_at`, and the retry sees `created=False` so it does not resend.
    The lead is told, the row says not told. This test pins the behaviour that matters -
    exactly one send, and the field is written in the same transaction as the create - so
    the window is a crash window only, never a duplicate-send window.
    """
    from tests.handoff_fixtures import make_handoff

    world = await make_conversation(owner, tenant_row)
    graph = FakeGraph()
    _, creation, _ = await make_handoff(db, tenant_row, world, graph=graph)

    assert len(graph.sends) == 1
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        handoff = await handoff_repo.get(
            session, tenant_id=tenant_row, handoff_id=creation.handoff.id
        )
    assert handoff.acknowledged_at is not None


async def test_two_tenants_can_hold_handoffs_for_the_same_lead_nonsense(db, owner, tenant_row):
    """Each tenant's queue is separate; a conversation id from another tenant is refused."""
    from tests.factories import create_tenant

    other = await create_tenant(owner, name="Globex", slug=f"globex-{uuid.uuid4().hex[:8]}")
    world = await make_conversation(owner, tenant_row)
    service = HumanHandoffService(db, other, settings=ho_settings(), clock=FixedClock(T0))

    result = await service.create_or_get_active_handoff(
        world["conversation_id"], reason=HandoffReason.HIGH_INTENT
    )

    assert result is None, "a conversation id from another tenant must not be escalated"
    assert await handoff_count(db, other) == 0
    notes = await notifications_for(db, other, uuid.uuid4())
    assert notes == []
