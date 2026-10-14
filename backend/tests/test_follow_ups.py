"""Phase 7: the follow-up state machine, the cancellation invariant, and tenancy.

Real PostgreSQL throughout. Time is advanced with the `clock` fixture - no test here
sleeps, and none of them would be runnable if they did.

One pattern throughout, because it is the real one: to move the sequence forward a test
schedules, advances the clock, and then calls `claim_and_execute`. That is the poller's
path, and it is the only path that authorises a send - `FollowUpService.execute` refuses a
task nobody has claimed. A test that called `execute` directly would skip the step the
tests exist to cover.
"""

import asyncio
import uuid
from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.db.tenant_context import set_current_tenant
from app.models.conversation import Channel
from app.models.lead import Lead, LeadStatus
from app.models.scheduled_task import ScheduledTask, TaskStatus, TaskType
from app.models.tenant import Tenant
from app.repositories import scheduled_tasks as task_repo
from app.services.conversations import (
    REASON_META_UNAVAILABLE,
    REASON_WINDOW_CLOSED,
    ConversationEngine,
)
from app.services.follow_up_templates import load_sequence
from app.services.follow_ups import TAGGED_MESSAGING_TYPE
from app.services.meta_messaging import InboundMessage
from tests.fakes import FakeGraph
from tests.follow_up_fixtures import T0, claim_and_execute, make_conversation, service

HOURS_24, HOURS_72 = 24, 72


def _inbound(mid: str, text: str, *, participant: str = "1000000001") -> InboundMessage:
    """An `InboundMessage`, shaped the way `extract_inbound_messages` produces one."""
    return InboundMessage(
        participant_id=participant,
        meta_message_id=mid,
        source_id=None,
        text=text,
        message_type="text",
        sent_at=None,
    )


async def _deliver(db, world, mid: str, text: str) -> None:
    """Run one inbound message through the engine, i.e. the real transaction.

    Targets `world`'s own participant. The fixture generates a unique one per conversation,
    so a hardcoded id would open a *second* thread and the follow-up would never be
    cancelled - a test that passes for entirely the wrong reason.
    """
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant_id"])
        await ConversationEngine(session, world["tenant_id"]).ingest_inbound(
            meta_connection_id=world["connection_id"],
            channel=Channel.MESSENGER,
            messages=[_inbound(mid, text, participant=world["participant_id"])],
        )


async def _tasks(db, tenant_id, conversation_id=None) -> dict:
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        statement = select(ScheduledTask).where(ScheduledTask.tenant_id == tenant_id)
        if conversation_id is not None:
            statement = statement.where(ScheduledTask.conversation_id == conversation_id)
        return {
            (t.task_type, t.sequence_step): t
            for t in (await session.execute(statement)).scalars().all()
        }


async def _all_tasks(db, tenant_id) -> list:
    """Every task for a tenant, as a list.

    `_tasks` keys by `(task_type, sequence_step)`, which is right for one conversation and
    wrong for several: four conversations' step-1 tasks would collapse into one entry and
    every count would silently be wrong.
    """
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_id)
        return list((await session.execute(select(ScheduledTask))).scalars().all())


def _start(db, world, clock, *, graph=None, **overrides):
    """Schedule step 1, as the AI pipeline does after a successful reply."""
    svc = service(db, world["tenant_id"], clock, graph=graph, **overrides)
    return svc


async def _arm(db, world, clock, *, graph=None, **overrides) -> FakeGraph:
    """Schedule step 1 with `graph` wired, and return the graph for assertions."""
    graph = graph or FakeGraph()
    await service(db, world["tenant_id"], clock, graph=graph, **overrides).on_reply_sent(
        world["conversation_id"]
    )
    return graph


async def _run_all_steps(db, world, clock, *, graph=None, **overrides) -> FakeGraph:
    """Walk FU1, FU2, FU3 to completion, advancing the clock between each."""
    graph = graph or FakeGraph()
    await _arm(db, world, clock, graph=graph, **overrides)
    for wait in (4, HOURS_24, HOURS_72):
        clock.advance(hours=wait)
        await claim_and_execute(db, world["tenant_id"], clock, graph=graph)
    return graph


# -- 1. scheduling -----------------------------------------------------------


async def test_an_eligible_conversation_schedules_follow_up_1(db, conversation, clock):
    task = await _start(db, conversation, clock).on_reply_sent(conversation["conversation_id"])
    assert task is not None
    assert task.task_type == TaskType.FOLLOW_UP
    assert task.sequence_step == 1
    assert task.status == TaskStatus.PENDING


async def test_follow_up_1_is_due_at_four_hours(db, conversation, clock):
    task = await _start(db, conversation, clock).on_reply_sent(conversation["conversation_id"])
    assert task.scheduled_at == T0 + timedelta(hours=4)


async def test_follow_up_2_is_scheduled_only_after_1_is_sent(db, conversation, clock):
    graph = await _arm(db, conversation, clock)
    # Only step 1 exists after the first reply. The sequence is not pre-created, so a
    # conversation that goes quiet never accumulates tasks nobody will run.
    assert set(await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])) == {
        (TaskType.FOLLOW_UP, 1)
    }
    clock.advance(hours=4)
    result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert result.completed == 1
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert steps[(TaskType.FOLLOW_UP, 2)].status == TaskStatus.PENDING


async def test_follow_up_3_is_scheduled_after_2(db, conversation, clock):
    graph = await _arm(db, conversation, clock)
    for wait in (4, HOURS_24):
        clock.advance(hours=wait)
        result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
        assert result.completed == 1
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert (TaskType.FOLLOW_UP, 3) in steps


async def test_step_delays_follow_the_prd_sequence(db, conversation, clock):
    """+4h, then +24h, then +72h - each measured from the previous *send*."""
    graph = await _arm(db, conversation, clock)
    # (step being run, the delay that produces the next step, the next step number)
    ladder = ((1, HOURS_24, 2), (2, HOURS_72, 3))
    due = {1: T0 + timedelta(hours=4)}
    for step, next_delay, next_step in ladder:
        clock.set(due[step])
        result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
        assert result.completed == 1
        steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
        nxt = steps[(TaskType.FOLLOW_UP, next_step)]
        assert nxt.scheduled_at == due[step] + timedelta(hours=next_delay)
        due[next_step] = nxt.scheduled_at


async def test_the_last_step_arms_the_ghost_evaluation(db, conversation, clock):
    await _run_all_steps(db, conversation, clock)
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert (TaskType.GHOST, 1) in steps


async def test_the_ghost_is_due_seven_days_after_the_sequence_started(db, conversation, clock):
    """The boundary is the PRD's: seven days of silence from the last real activity.

    The task is *created* when the final follow-up is scheduled, which is as soon as the
    previous one is sent - around day three. Anchoring the ghost to that moment would
    place the boundary near day ten. Asserted on the stored timestamp so the arithmetic
    cannot drift, and so this cannot pass merely because the ghost eventually came due.
    """
    await _run_all_steps(db, conversation, clock)
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])

    assert steps[(TaskType.GHOST, 1)].scheduled_at == T0 + timedelta(days=7)
    # ... and therefore after the last follow-up, which is the whole point: a lead is
    # given all three messages before being written off.
    assert steps[(TaskType.FOLLOW_UP, 3)].scheduled_at < steps[(TaskType.GHOST, 1)].scheduled_at


async def test_a_shortened_boundary_still_lets_the_final_follow_up_go_out(db, conversation, clock):
    """Shorten the boundary and it collides with the last follow-up. The lead still gets it.

    With the default seven-day boundary the final follow-up is due long before it, so the
    two never collide. At one day the clamp pulls the ghost up to the final step's due
    time instead, making both due in the same poll - while still inside the 7-day Meta
    window, so the final send is genuinely attempted rather than correctly refused.
    """
    ghosted_days = 1
    graph = await _arm(
        db, conversation, clock, graph=FakeGraph(), follow_up_ghosted_days=ghosted_days
    )
    for wait in (4, HOURS_24, HOURS_72):
        clock.advance(hours=wait)
        await claim_and_execute(
            db,
            conversation["tenant_id"],
            clock,
            graph=graph,
            follow_up_ghosted_days=ghosted_days,
        )

    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert steps[(TaskType.GHOST, 1)].scheduled_at == steps[(TaskType.FOLLOW_UP, 3)].scheduled_at
    # The lead received the whole sequence *and* was then written off. Being ghosted must
    # never be what prevented the last message.
    assert steps[(TaskType.FOLLOW_UP, 3)].status == TaskStatus.COMPLETED
    assert steps[(TaskType.GHOST, 1)].status == TaskStatus.COMPLETED
    assert len(graph.sends) == 3

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        lead = await session.get(Lead, conversation["lead_id"])
    assert lead.status == LeadStatus.GHOSTED


async def test_a_ghost_reached_with_a_follow_up_still_owed_defers(db, conversation, clock):
    """The guard itself, independent of claim ordering.

    `claim_due` orders by due time, so when the clamp makes the ghost and the final
    follow-up due at the same instant, which one the poller dispatches first is a tie the
    database breaks. This arms a ghost *before* the outstanding follow-up instead, so the
    guard is what decides the outcome rather than the ordering.
    """
    graph = await _arm(db, conversation, clock, graph=FakeGraph())
    # Send follow-up #1, leaving #2 pending and not yet due.
    clock.advance(hours=4)
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        await task_repo.schedule(
            session,
            tenant_id=conversation["tenant_id"],
            conversation_id=conversation["conversation_id"],
            lead_id=conversation["lead_id"],
            task_type=TaskType.GHOST,
            sequence_step=1,
            scheduled_at=clock.now(),
            max_attempts=3,
            activity_anchor_at=T0,
        )

    # Only the ghost is due, so the guard is definitely what runs.
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert steps[(TaskType.FOLLOW_UP, 2)].status == TaskStatus.PENDING
    assert steps[(TaskType.GHOST, 1)].status == TaskStatus.PENDING

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        lead = await session.get(Lead, conversation["lead_id"])
    assert lead.status is not LeadStatus.GHOSTED

    # With nothing outstanding, the boundary applies as it normally would.
    for wait in (HOURS_24, HOURS_72):
        clock.advance(hours=wait)
        await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)

    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert steps[(TaskType.GHOST, 1)].status == TaskStatus.COMPLETED
    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        lead = await session.get(Lead, conversation["lead_id"])
    assert lead.status == LeadStatus.GHOSTED


async def test_the_sequence_terminates_after_the_last_step(db, conversation, clock):
    await _run_all_steps(db, conversation, clock)
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    follow_ups = [k for k in steps if k[0] == TaskType.FOLLOW_UP]
    assert follow_ups == [
        (TaskType.FOLLOW_UP, 1),
        (TaskType.FOLLOW_UP, 2),
        (TaskType.FOLLOW_UP, 3),
    ]


async def test_duplicate_scheduling_is_idempotent(db, conversation, clock):
    svc = _start(db, conversation, clock)
    for _ in range(3):
        await svc.on_reply_sent(conversation["conversation_id"])
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert [k for k in steps if k[0] == TaskType.FOLLOW_UP] == [(TaskType.FOLLOW_UP, 1)]


async def test_concurrent_scheduling_creates_one_task(db, conversation, clock):
    svc = _start(db, conversation, clock)
    await asyncio.gather(*(svc.on_reply_sent(conversation["conversation_id"]) for _ in range(6)))
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert [k for k in steps if k[0] == TaskType.FOLLOW_UP] == [(TaskType.FOLLOW_UP, 1)]


async def test_a_completed_step_is_not_rescheduled_on_a_later_reply(db, conversation, clock):
    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    await _start(db, conversation, clock, graph=graph).on_reply_sent(
        conversation["conversation_id"]
    )
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert steps[(TaskType.FOLLOW_UP, 1)].status == TaskStatus.COMPLETED
    assert len(graph.sends) == 1


async def test_nothing_is_scheduled_when_follow_ups_are_disabled(db, conversation, clock):
    svc = _start(db, conversation, clock, follow_up_enabled=False)
    assert await svc.on_reply_sent(conversation["conversation_id"]) is None
    assert await _tasks(db, conversation["tenant_id"], conversation["conversation_id"]) == {}


async def test_a_conversation_with_no_inbound_is_not_eligible(db, owner, tenant_row, clock):
    from app.models.conversation import Conversation

    world = await make_conversation(owner, tenant_row)
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        conversation = await session.get(Conversation, world["conversation_id"])
        conversation.last_inbound_at = None
    svc = _start(db, world, clock)
    assert await svc.on_reply_sent(world["conversation_id"]) is None


async def test_a_tenant_may_override_delays_and_templates(db, owner, tenant_row, clock):
    world = await make_conversation(owner, tenant_row)
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        tenant = await session.get(Tenant, tenant_row)
        tenant.follow_up_config = {
            "1": {
                "delay_hours": 2,
                "template": "Hey {first_name}, custom step one.",
                "ai_enhance": False,
            }
        }
    task = await _start(db, world, clock).on_reply_sent(world["conversation_id"])
    assert task.scheduled_at == T0 + timedelta(hours=2)
    # Only the configured step exists, so the sequence ends after it - and the ghost
    # evaluation is armed then, because the last configured step just went out.
    steps = await _tasks(db, tenant_row, world["conversation_id"])
    assert set(steps) == {(TaskType.FOLLOW_UP, 1), (TaskType.GHOST, 1)}


# -- 2. execution ------------------------------------------------------------


async def test_executing_a_due_task_sends_through_the_existing_engine(db, conversation, clock):
    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)
    result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert result.completed == 1
    assert len(graph.sends) == 1
    # The default template greets the lead by the first name from the Lead record.
    assert "Dana" in graph.sends[0]["text"]


async def test_the_send_persists_an_outbound_message(db, conversation, clock):
    from app.models.conversation import Message, MessageDirection

    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        rows = (
            (
                await session.execute(
                    select(Message).where(
                        Message.conversation_id == conversation["conversation_id"],
                        Message.direction == MessageDirection.OUTBOUND,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(rows) == 1
    assert rows[0].delivery.value == "SENT"
    # No AI attribution: the deterministic template was used, so naming a model would be
    # a lie.
    assert rows[0].ai_model is None


async def test_the_poller_does_not_claim_a_task_before_it_is_due(db, conversation, clock):
    graph = await _arm(db, conversation, clock)
    clock.advance(hours=3)
    result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert result.claimed == 0
    assert graph.sends == []


async def test_an_unknown_task_is_skipped_rather_than_raising(db, conversation, clock):
    outcome = await _start(db, conversation, clock).execute(uuid.uuid4())
    assert outcome.status == "SKIPPED"


async def test_a_task_nobody_claimed_is_skipped(db, conversation, clock):
    """Only the poller may claim, and only a claim authorises a send."""
    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)
    outcome = await service(db, conversation["tenant_id"], clock, graph=graph).execute(
        (await _tasks(db, conversation["tenant_id"]))[(TaskType.FOLLOW_UP, 1)].id
    )
    assert outcome.status == "SKIPPED"
    assert outcome.reason == "not_claimed"
    assert graph.sends == []


# -- 3. the critical cancellation invariant ----------------------------------


async def test_an_inbound_message_cancels_a_pending_follow_up(db, conversation, clock):
    await _arm(db, conversation, clock)
    await _deliver(db, conversation, "mid.reply.1", "Yes, send me details")
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert steps[(TaskType.FOLLOW_UP, 1)].status == TaskStatus.CANCELLED
    assert steps[(TaskType.FOLLOW_UP, 1)].cancelled_at is not None


async def test_an_inbound_message_cancels_every_pending_step(db, conversation, clock):
    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    clock.advance(hours=HOURS_24)
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    # FU3 and the ghost evaluation are both pending at this point.
    assert steps[(TaskType.FOLLOW_UP, 3)].status == TaskStatus.PENDING
    assert steps[(TaskType.GHOST, 1)].status == TaskStatus.PENDING

    await _deliver(db, conversation, "mid.reply.2", "interested")

    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert steps[(TaskType.FOLLOW_UP, 3)].status == TaskStatus.CANCELLED
    assert steps[(TaskType.GHOST, 1)].status == TaskStatus.CANCELLED


async def test_cancellation_is_in_the_same_transaction_as_the_message(db, conversation, clock):
    """The invariant, proved by rollback rather than by reading the code.

    If the cancel were in a later transaction, forcing this one to fail would leave the
    message unstored *and* the follow-up still PENDING - exactly the state in which a
    scheduler would send an obsolete follow-up. Asserting the rollback catches exactly that.
    """
    from app.models.conversation import Message

    await _arm(db, conversation, clock)
    with pytest.raises(RuntimeError):
        async with db() as session:
            await session.begin()
            await set_current_tenant(session, conversation["tenant_id"])
            await ConversationEngine(session, conversation["tenant_id"]).ingest_inbound(
                meta_connection_id=conversation["connection_id"],
                channel=Channel.MESSENGER,
                messages=[_inbound("mid.reply.3", "hello")],
            )
            # Roll the whole thing back, as a crash between insert and commit would.
            raise RuntimeError("simulated failure before commit")

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        replies = (
            (
                await session.execute(
                    select(Message).where(
                        Message.conversation_id == conversation["conversation_id"],
                        Message.meta_message_id == "mid.reply.3",
                    )
                )
            )
            .scalars()
            .all()
        )
    assert replies == []
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    # Rolled back with the message, so the follow-up is untouched and still legitimately
    # pending.
    assert steps[(TaskType.FOLLOW_UP, 1)].status == TaskStatus.PENDING


async def test_a_cancelled_task_never_sends(db, conversation, clock):
    graph = await _arm(db, conversation, clock)
    await _deliver(db, conversation, "mid.reply.4", "yes please")
    clock.advance(hours=4)
    result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert result.claimed == 0
    assert graph.sends == []


async def test_a_duplicate_inbound_delivery_does_not_resurrect_the_sequence(
    db, conversation, clock
):
    """Cancellation is idempotent, so a Meta retry changes nothing."""
    await _arm(db, conversation, clock)
    await _deliver(db, conversation, "mid.dup", "hi")
    await _deliver(db, conversation, "mid.dup", "hi")
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert steps[(TaskType.FOLLOW_UP, 1)].status == TaskStatus.CANCELLED


async def test_a_late_duplicate_does_not_cancel_a_sequence_armed_after_it(db, conversation, clock):
    """The gap the back-to-back test above cannot see.

    Cancelling is idempotent, which is why a duplicate delivered immediately after the
    original looked harmless. Meta retries are not immediate, so the reachable ordering is
    this one: the reply cancels step 1, step 1 sends and arms step 2, and only *then* does
    the retry of the original message arrive. Cancelling again there would stop the
    sequence for a lead who never replied again.
    """
    graph = FakeGraph()
    # The genuine reply first. There is nothing pending yet, so it cancels nothing - which is
    # the normal shape, because the sequence is armed *after* the AI's first response.
    await _deliver(db, conversation, "mid.late", "still interested?")

    await _arm(db, conversation, clock, graph=graph)
    clock.advance(hours=4)
    result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert result.claimed == 1
    assert len(graph.sends) == 1

    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert steps[(TaskType.FOLLOW_UP, 2)].status == TaskStatus.PENDING

    # Meta redelivers the *original* message, well after the fact.
    await _deliver(db, conversation, "mid.late", "still interested?")

    after = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert after[(TaskType.FOLLOW_UP, 2)].status == TaskStatus.PENDING, (
        "a late retry of an old message cancelled a live follow-up sequence"
    )


# -- 4. the cancellation race -----------------------------------------------


async def _claim_one(db, world, clock, worker: str):
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant_id"])
        return await task_repo.claim_due(
            session,
            tenant_id=world["tenant_id"],
            now=clock.now(),
            limit=10,
            worker=worker,
            lease_seconds=300,
        )


async def test_a_claimed_task_is_still_cancelled_if_a_reply_arrives(db, conversation, clock):
    """THE race. A worker claims; the lead replies; the worker must not send.

    The cancel `UPDATE` only touches PENDING rows, so a claimed PROCESSING task is
    untouched - which is exactly why the execution path re-checks eligibility. Without
    that second check this test fails and a real lead receives a "just checking in" after
    they had already replied.
    """
    from app.models.conversation import Conversation

    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)
    assert await _claim_one(db, conversation, clock, "worker-a")

    # The inbound transaction's cancel cannot touch a PROCESSING row.
    await _deliver(db, conversation, "mid.race", "just replying")
    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        row = await session.get(Conversation, conversation["conversation_id"])
        steps = await _tasks(db, conversation["tenant_id"])
        assert row.last_inbound_at > steps[(TaskType.FOLLOW_UP, 1)].activity_anchor_at

    # Worker A now proceeds. A second poll finds nothing - A already holds the claim - and
    # nothing is sent.
    second_poll = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert second_poll.claimed == 0
    assert graph.sends == []

    # `worker="worker-a"` is the identity the claim was taken under. Executing without it
    # would make the service a *different* worker as far as the lease is concerned, and it
    # would correctly refuse - which is the property the next test covers, not this one.
    outcome = await service(
        db, conversation["tenant_id"], clock, graph=graph, worker="worker-a"
    ).execute((await _tasks(db, conversation["tenant_id"]))[(TaskType.FOLLOW_UP, 1)].id)
    assert outcome.status == "CANCELLED"
    assert outcome.reason == "lead_replied"
    assert graph.sends == [], "a follow-up was sent to a lead who had already replied"
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert steps[(TaskType.FOLLOW_UP, 1)].status == TaskStatus.CANCELLED


async def test_the_poller_race_sends_nothing_obsolete(db, conversation, clock):
    """Same race, but the task is still PENDING when the reply lands."""
    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)
    await _deliver(db, conversation, "mid.race2", "replying now")
    result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert result.claimed == 0
    assert graph.sends == []


# -- 5. claiming and multi-worker safety -------------------------------------


async def test_two_workers_claim_one_task_and_one_executes(db, conversation, clock):
    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)
    results = await asyncio.gather(
        *(claim_and_execute(db, conversation["tenant_id"], clock, graph=graph) for _ in range(2))
    )
    assert sum(r.claimed for r in results) == 1
    assert sum(r.completed for r in results) == 1
    assert len(graph.sends) == 1


async def test_ten_workers_claim_one_task_and_only_one_executes(db, conversation, clock):
    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)
    results = await asyncio.gather(
        *(claim_and_execute(db, conversation["tenant_id"], clock, graph=graph) for _ in range(10))
    )
    assert sum(r.claimed for r in results) == 1
    assert sum(r.completed for r in results) == 1
    # The assertion that matters: exactly one outbound message, not ten.
    assert len(graph.sends) == 1


async def test_claiming_respects_the_batch_size(db, owner, tenant_row, clock):
    """A poll takes a bounded slice, so one busy tenant cannot monopolise a worker."""
    graph = FakeGraph()
    worlds = [await make_conversation(owner, tenant_row) for _ in range(4)]
    for world in worlds:
        await _arm(db, world, clock, graph=graph)
    clock.advance(hours=4)
    result = await claim_and_execute(db, tenant_row, clock, graph=graph, follow_up_batch_size=2)
    assert result.claimed == 2
    assert len(graph.sends) == 2
    steps = await _all_tasks(db, tenant_row)
    # The two that ran are COMPLETED, and each armed its own next step; the other two are
    # untouched and still PENDING.
    assert result.completed == 2, result
    assert len([t for t in steps if t.status == TaskStatus.COMPLETED]) == 2
    assert len([t for t in steps if t.sequence_step == 1 and t.status == TaskStatus.PENDING]) == 2


# -- 6. recovery -------------------------------------------------------------


async def test_a_pending_task_survives_a_worker_restart(db, conversation, clock):
    """A restart is a non-event: the row is the state, the process is not."""
    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)
    # Nothing has run. A brand-new scheduler - as after a container replace - picks it up.
    result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert result.claimed == 1
    assert result.completed == 1
    assert len(graph.sends) == 1


async def test_a_stale_processing_task_is_reclaimed_after_the_lease(db, conversation, clock):
    """A worker that died mid-execute must not strand the task forever."""
    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)
    assert await _claim_one(db, conversation, clock, "dead-worker")
    # Still inside the lease: not reclaimable yet.
    assert (await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)).claimed == 0
    clock.advance(seconds=301)
    result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert result.claimed == 1
    assert result.completed == 1
    assert len(graph.sends) == 1


async def test_the_lease_prevents_a_second_execution_before_it_expires(db, conversation, clock):
    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)
    assert await _claim_one(db, conversation, clock, "slow-worker")
    for _ in range(3):
        clock.advance(seconds=60)
        assert (
            await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
        ).claimed == 0
    assert graph.sends == []


# -- 7. eligibility ----------------------------------------------------------


@pytest.mark.parametrize(
    "status", [LeadStatus.HUMAN_HANDOFF, LeadStatus.CLOSED, LeadStatus.GHOSTED]
)
async def test_a_blocked_lead_status_prevents_a_follow_up(db, owner, tenant_row, clock, status):
    world = await make_conversation(owner, tenant_row, lead_status=status)
    svc = _start(db, world, clock)
    assert await svc.on_reply_sent(world["conversation_id"]) is None


async def test_qualified_does_not_block_a_follow_up(db, owner, tenant_row, clock):
    """Only terminal states block. A qualified lead still gets nudged."""
    world = await make_conversation(owner, tenant_row, lead_status=LeadStatus.QUALIFIED)
    svc = _start(db, world, clock)
    assert await svc.on_reply_sent(world["conversation_id"]) is not None


async def test_a_conversation_with_no_lead_is_still_followed_up(db, owner, tenant_row, clock):
    """Someone messaging a page without submitting an ad form is still a person."""
    world = await make_conversation(owner, tenant_row, with_lead=False)
    graph = await _arm(db, world, clock)
    clock.advance(hours=4)
    result = await claim_and_execute(db, tenant_row, clock, graph=graph)
    assert result.completed == 1
    # No lead means no first name, so the placeholder renders as nothing.
    assert "{" not in graph.sends[0]["text"]


async def test_a_task_whose_step_is_no_longer_configured_is_cancelled(db, owner, tenant_row, clock):
    world = await make_conversation(owner, tenant_row)
    graph = await _arm(db, world, clock)
    # The tenant removes the step after it was scheduled.
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        tenant = await session.get(Tenant, tenant_row)
        tenant.follow_up_config = {
            "2": {"delay_hours": 24, "template": "only step two", "ai_enhance": False}
        }
    clock.advance(hours=4)
    result = await claim_and_execute(db, tenant_row, clock, graph=graph)
    assert result.skipped == 1
    assert result.outcomes[0].reason == "step_not_configured"
    assert graph.sends == []


# -- 8. the Meta messaging window -------------------------------------------


async def test_inside_24h_uses_the_response_path(db, conversation, clock):
    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert graph.sends[0]["messaging_type"] == "RESPONSE"
    assert graph.sends[0].get("tag") is None


async def test_between_24h_and_7d_uses_the_tagged_path(db, conversation, clock):
    """Follow-up #2 lands outside the standard window, so it needs MESSAGE_TAG."""
    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    clock.advance(hours=HOURS_24)
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert graph.sends[1]["messaging_type"] == TAGGED_MESSAGING_TYPE
    assert graph.sends[1]["tag"] == "HUMAN_AGENT"


async def test_beyond_7d_sends_nothing_at_all(db, owner, tenant_row, clock):
    graph = FakeGraph()
    world = await make_conversation(owner, tenant_row, last_inbound_at=T0 - timedelta(days=8))
    # Not even schedulable: there is no open window to reply inside.
    assert (
        await _start(db, world, clock, graph=graph).on_reply_sent(world["conversation_id"]) is None
    )
    assert graph.sends == []


async def test_a_window_closed_rejection_is_permanent(db, conversation, clock):
    from app.services.meta_graph import MetaMessageWindowClosed

    graph = FakeGraph()
    graph.program_send_error(MetaMessageWindowClosed("window closed"))
    await _arm(db, conversation, clock, graph=graph)
    clock.advance(hours=4)
    result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert result.failed == 1
    assert result.outcomes[0].reason == REASON_WINDOW_CLOSED
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    # FAILED, not PENDING: a rejected send is not retried.
    assert steps[(TaskType.FOLLOW_UP, 1)].status == TaskStatus.FAILED


async def test_a_disconnected_connection_is_permanent(db, conversation, clock):
    from app.models.meta_connection import MetaConnection

    graph = FakeGraph()
    await _arm(db, conversation, clock, graph=graph)
    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        connection = await session.get(MetaConnection, conversation["connection_id"])
        connection.access_token_encrypted = None
    clock.advance(hours=4)
    result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert result.failed == 1
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert steps[(TaskType.FOLLOW_UP, 1)].status == TaskStatus.FAILED


# -- 9. failure and retry ----------------------------------------------------


async def test_an_ambiguous_meta_failure_is_not_blindly_resent(db, conversation, clock):
    """Meta may have accepted a message whose response never arrived.

    Resending risks two follow-ups to one lead, which is worse than one missed follow-up.
    So an *unknown outcome* is terminal, not retried.

    Phase 10 split this out from a plain rate limit. A timeout now raises
    `MetaUnknownOutcomeError` and is recorded as `delivery_outcome_unknown`, because "we never
    saw the answer" and "Meta said it did not take the request" are opposite situations with
    opposite retry answers. `test_a_rate_limited_send_is_retried` covers the other half.
    """
    from app.services.conversations import REASON_OUTCOME_UNKNOWN
    from app.services.meta_graph import MetaUnknownOutcomeError

    graph = FakeGraph()
    graph.program_send_error(MetaUnknownOutcomeError("upstream timeout"))
    await _arm(db, conversation, clock, graph=graph)
    clock.advance(hours=4)
    result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert result.failed == 1
    assert result.outcomes[0].reason == REASON_OUTCOME_UNKNOWN
    assert len(graph.sends) == 1

    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert steps[(TaskType.FOLLOW_UP, 1)].status == TaskStatus.FAILED

    # Advancing well past any backoff must not resend it.
    clock.advance(days=1)
    later = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert later.claimed == 0
    assert len(graph.sends) == 1, "an ambiguous send was retried and could have duplicated"


async def test_a_rate_limited_send_is_retried(db, conversation, clock):
    """The other half of the split: Meta *answered* and said it did not take the request.

    Nothing was delivered, so a bounded retry cannot duplicate anything, and treating a
    temporary provider outage as terminal would silently drop follow-ups whenever Meta rate
    limits us.
    """
    from app.services.meta_graph import MetaTransientError

    graph = FakeGraph()
    graph.program_send_error(MetaTransientError("rate limited"))
    await _arm(db, conversation, clock, graph=graph)
    clock.advance(hours=4)
    result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)

    assert result.failed == 1
    assert result.outcomes[0].reason == REASON_META_UNAVAILABLE
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    task = steps[(TaskType.FOLLOW_UP, 1)]
    assert task.status == TaskStatus.PENDING, "a rate limit is safely retryable"
    assert task.scheduled_at > clock.now(), "the retry must be backed off, not hot-looped"


async def test_a_crashed_execution_leaves_the_task_recoverable(
    db, conversation, clock, monkeypatch
):
    """A crash is not a verdict; the lease must return the task to service."""
    from app.services.follow_up_scheduler import FollowUpScheduler
    from tests.follow_up_fixtures import fu_settings

    graph = FakeGraph()
    await _arm(db, conversation, clock, graph=graph)
    clock.advance(hours=4)

    async def _boom(self, task_id):
        raise RuntimeError("process died")

    monkeypatch.setattr("app.services.follow_up_scheduler.FollowUpService.execute", _boom)
    result = await FollowUpScheduler(
        db, conversation["tenant_id"], clock=clock, graph=graph, settings=fu_settings()
    ).poll_once()
    assert result.claimed == 1
    assert result.failed == 1
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    # Still PROCESSING, not FAILED: we do not know whether the send happened.
    assert steps[(TaskType.FOLLOW_UP, 1)].status == TaskStatus.PROCESSING

    monkeypatch.undo()
    clock.advance(seconds=301)
    recovered = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert recovered.completed == 1
    assert len(graph.sends) == 1


# -- 10. ghosted -------------------------------------------------------------


async def _arm_ghost_directly(db, world, clock) -> None:
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant_id"])
        await task_repo.schedule(
            session,
            tenant_id=world["tenant_id"],
            conversation_id=world["conversation_id"],
            lead_id=world["lead_id"],
            task_type=TaskType.GHOST,
            sequence_step=1,
            scheduled_at=clock.now(),
            max_attempts=3,
            activity_anchor_at=T0,
        )


async def test_a_silent_lead_becomes_ghosted_after_seven_days(db, conversation, clock):
    graph = await _arm(db, conversation, clock, graph=FakeGraph())
    # Walk the whole sequence, then jump past the ghost boundary.
    for wait in (4, HOURS_24, HOURS_72):
        clock.advance(hours=wait)
        await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    clock.advance(days=7)
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        assert (await session.get(Lead, conversation["lead_id"])).status == LeadStatus.GHOSTED


async def test_the_ghost_transition_is_idempotent(db, conversation, clock):
    graph = await _arm(db, conversation, clock, graph=FakeGraph())
    for wait in (4, HOURS_24, HOURS_72):
        clock.advance(hours=wait)
        await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    clock.advance(days=7)
    # Two more passes must not corrupt anything.
    for _ in range(2):
        await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        assert (await session.get(Lead, conversation["lead_id"])).status == LeadStatus.GHOSTED


async def test_a_lead_who_replied_is_never_marked_ghosted(db, conversation, clock):
    graph = await _arm(db, conversation, clock, graph=FakeGraph())
    for wait in (4, HOURS_24, HOURS_72):
        clock.advance(hours=wait)
        await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    # A reply on day 6 cancels the pending ghost evaluation.
    clock.set(T0 + timedelta(days=6))
    await _deliver(db, conversation, "mid.day6", "still interested")
    clock.advance(days=2)
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        assert (await session.get(Lead, conversation["lead_id"])).status != LeadStatus.GHOSTED


async def test_ghost_does_not_overwrite_human_handoff(db, owner, tenant_row, clock):
    world = await make_conversation(owner, tenant_row, lead_status=LeadStatus.REPLIED)
    await _arm_ghost_directly(db, world, clock)
    # A human picks the thread up in the meantime.
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        lead = await session.get(Lead, world["lead_id"])
        lead.status = LeadStatus.HUMAN_HANDOFF

    clock.advance(days=1)
    result = await claim_and_execute(db, tenant_row, clock, graph=FakeGraph())
    assert result.skipped == 1

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        assert (await session.get(Lead, world["lead_id"])).status == LeadStatus.HUMAN_HANDOFF


async def test_ghost_does_not_overwrite_closed(db, owner, tenant_row, clock):
    world = await make_conversation(owner, tenant_row, lead_status=LeadStatus.REPLIED)
    await _arm_ghost_directly(db, world, clock)
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        lead = await session.get(Lead, world["lead_id"])
        lead.status = LeadStatus.CLOSED

    clock.advance(days=1)
    await claim_and_execute(db, tenant_row, clock, graph=FakeGraph())

    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        assert (await session.get(Lead, world["lead_id"])).status == LeadStatus.CLOSED


# -- 11. tenant isolation ----------------------------------------------------


async def test_a_tenant_cannot_see_another_tenants_tasks(db, conversation, other_tenant_row, clock):
    await _arm(db, conversation, clock)
    async with db() as session, session.begin():
        await set_current_tenant(session, other_tenant_row)
        assert (await session.execute(select(ScheduledTask))).scalars().all() == []


async def test_a_tenant_cannot_claim_another_tenants_task(
    db, conversation, other_tenant_row, clock
):
    await _arm(db, conversation, clock)
    clock.advance(hours=4)
    async with db() as session, session.begin():
        await set_current_tenant(session, other_tenant_row)
        claimed = await task_repo.claim_due(
            session,
            tenant_id=other_tenant_row,
            now=clock.now(),
            limit=10,
            worker="intruder",
            lease_seconds=300,
        )
    assert claimed == []
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert steps[(TaskType.FOLLOW_UP, 1)].status == TaskStatus.PENDING


async def test_a_tenant_cannot_cancel_another_tenants_task(
    db, conversation, other_tenant_row, clock
):
    await _arm(db, conversation, clock)
    async with db() as session, session.begin():
        await set_current_tenant(session, other_tenant_row)
        cancelled = await task_repo.cancel_pending_for_conversation(
            session,
            tenant_id=other_tenant_row,
            conversation_id=conversation["conversation_id"],
            now=clock.now(),
        )
    assert cancelled == 0
    steps = await _tasks(db, conversation["tenant_id"], conversation["conversation_id"])
    assert steps[(TaskType.FOLLOW_UP, 1)].status == TaskStatus.PENDING


async def test_rls_denies_cross_tenant_task_access_in_raw_sql(db, conversation, clock):
    """Verified in SQL, not through the repository, so the policy is what is under test."""
    await _arm(db, conversation, clock)
    async with db() as session, session.begin():
        await set_current_tenant(session, uuid.UUID(int=0xABCD))
        rows = (await session.execute(text("SELECT * FROM scheduled_tasks"))).all()
    assert rows == []


async def test_a_cross_tenant_task_insert_is_refused(db, conversation, other_tenant_row):
    with pytest.raises(Exception, match="row-level security|foreign key"):
        async with db() as session, session.begin():
            await set_current_tenant(session, other_tenant_row)
            session.add(
                ScheduledTask(
                    tenant_id=other_tenant_row,
                    conversation_id=conversation["conversation_id"],
                    task_type=TaskType.FOLLOW_UP,
                    sequence_step=99,
                    scheduled_at=T0,
                    status=TaskStatus.PENDING,
                    attempt_count=0,
                    max_attempts=3,
                )
            )


async def test_scheduled_tasks_has_forced_rls_and_the_tenant_policy(db):
    async with db() as session, session.begin():
        row = (
            await session.execute(
                text(
                    "SELECT c.relrowsecurity, c.relforcerowsecurity FROM pg_class c "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname = 'scheduled_tasks' AND n.nspname = 'public'"
                )
            )
        ).one()
        policies = (
            (
                await session.execute(
                    text("SELECT policyname FROM pg_policies WHERE tablename = 'scheduled_tasks'")
                )
            )
            .scalars()
            .all()
        )
    assert row.relrowsecurity is True
    assert row.relforcerowsecurity is True
    assert len(policies) == 4


async def test_the_scheduler_only_polls_its_own_tenant(db, conversation, other_tenant_row, clock):
    await _arm(db, conversation, clock)
    clock.advance(hours=4)
    graph = FakeGraph()
    result = await claim_and_execute(db, other_tenant_row, clock, graph=graph)
    assert result.claimed == 0
    assert graph.sends == []


# -- 12. templates -----------------------------------------------------------


def test_the_default_sequence_is_the_prds():
    sequence = load_sequence(None)
    assert sequence.is_default is True
    assert [sequence.step(n).delay_hours for n in (1, 2, 3)] == [4, 24, 72]


def test_an_unknown_placeholder_is_removed_rather_than_sent():
    from app.services.follow_up_templates import render

    text, unknown = render("Hi {first_name}, your {discount_code} is ready", first_name="Dana")
    assert "Dana" in text
    assert "discount_code" not in text
    assert unknown == ["discount_code"]


def test_only_first_name_is_supported():
    from app.services.follow_up_templates import ALLOWED_PLACEHOLDERS

    assert ALLOWED_PLACEHOLDERS == {"first_name"}


@pytest.mark.parametrize(
    "hostile",
    ["{{ config.items() }}", "${7*7}", "{% for x in y %}", "{first_name} {{ secrets }}"],
)
def test_a_template_cannot_contain_template_engine_syntax(hostile):
    from app.services.follow_up_templates import TemplateStep

    with pytest.raises(ValueError):
        TemplateStep(delay_hours=4, template=hostile)


def test_an_invalid_tenant_config_is_rejected_rather_than_half_applied():
    with pytest.raises(ValueError):
        load_sequence({"1": {"delay_hours": 4, "template": "ok"}, "two": {"x": 1}})


def test_the_first_name_is_the_first_word():
    from app.services.follow_up_templates import first_name_from

    assert first_name_from("Dana Buyer") == "Dana"
    assert first_name_from("  Dana  ") == "Dana"
    assert first_name_from("") is None
    assert first_name_from(None) is None


async def test_a_tenant_template_is_used_verbatim(db, owner, tenant_row, clock):
    world = await make_conversation(owner, tenant_row)
    async with db() as session, session.begin():
        await set_current_tenant(session, tenant_row)
        tenant = await session.get(Tenant, tenant_row)
        tenant.follow_up_config = {
            "1": {
                "delay_hours": 4,
                "template": "Good morning {first_name}, a bespoke note.",
                "ai_enhance": False,
            }
        }
    graph = await _arm(db, world, clock)
    clock.advance(hours=4)
    await claim_and_execute(db, tenant_row, clock, graph=graph)
    assert graph.sends[0]["text"] == "Good morning Dana, a bespoke note."


async def test_a_lead_without_a_name_gets_no_placeholder(db, owner, tenant_row, clock):
    world = await make_conversation(owner, tenant_row, lead_name=None)
    graph = await _arm(db, world, clock)
    clock.advance(hours=4)
    await claim_and_execute(db, tenant_row, clock, graph=graph)
    assert "{" not in graph.sends[0]["text"]
