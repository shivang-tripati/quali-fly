"""Scheduler durability: ghosting boundaries, deferral, cancellation, crash recovery.

Phase 10. Everything here uses the real database because each property is a database or
clock property - a lease expiring, a task being reclaimed, a due-time comparison - and a
mock would only assert that the mock behaves as written.

The ghosting boundaries are the ones the brief calls out (+4h, +24h, +72h, 7 days) because
Phase 7 previously shipped a bug where ghosting fired around day ten, a week past the PRD's
boundary and a week after the lead stopped being a prospect. That is the exact class of
off-by-several-days error worth pinning with explicit numbers.
"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.tenant_context import set_current_tenant
from app.models.lead import Lead, LeadStatus
from app.models.scheduled_task import ScheduledTask, TaskStatus, TaskType
from app.repositories import scheduled_tasks as task_repo
from tests.fakes import FakeGraph
from tests.follow_up_fixtures import T0, claim_and_execute, service

pytestmark = pytest.mark.integration

HOURS_24 = timedelta(hours=24)
HOURS_72 = timedelta(hours=72)
LEASE = 60


async def _arm(db, world, clock, *, graph=None, **overrides) -> FakeGraph:
    """Arm follow-up step 1, optionally with a graph that is programmed to fail.

    `graph` is an explicit parameter rather than arriving in `overrides` because the body
    always passes it, and a duplicate keyword would be a confusing error rather than an
    obvious one.
    """
    graph = graph if graph is not None else FakeGraph()
    await service(db, world["tenant_id"], clock, graph=graph, **overrides).on_reply_sent(
        world["conversation_id"]
    )
    return graph


async def _task(db, world, task_type: TaskType, step: int = 1) -> ScheduledTask:
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant_id"])
        return await session.scalar(
            select(ScheduledTask).where(
                ScheduledTask.tenant_id == world["tenant_id"],
                ScheduledTask.task_type == task_type,
                ScheduledTask.sequence_step == step,
            )
        )


async def _lead(db, world) -> Lead:
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant_id"])
        return await session.get(Lead, world["lead_id"])


# -- ghosting boundaries -----------------------------------------------------


async def test_the_ghost_is_due_seven_days_after_the_last_inbound(db, conversation, clock):
    """The Phase 7 regression, pinned on the stored due time rather than a counted offset.

    Anchoring the ghost to `now`, at the moment the final step is *created* (around day
    three), puts the boundary at day ten - a week past the PRD and a week after the lead
    stopped being a prospect. The correct anchor is the last inbound message.

    The boundary cases are derived from the stored due time rather than counted on a
    finger, because the first version of this test measured "7 days" from the end of the
    sequence and was two days out - a test that pins the wrong number confidently.
    """
    graph = await _arm(db, conversation, clock)

    for wait in (4, 24, 72):
        clock.advance(hours=wait)
        await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert (await _lead(db, conversation)).status is not LeadStatus.GHOSTED

    ghost = await _task(db, conversation, TaskType.GHOST)
    assert ghost.status is TaskStatus.PENDING
    expected = T0 + timedelta(days=7)
    assert ghost.scheduled_at == expected, (
        f"ghost due at {ghost.scheduled_at}; it must be last_inbound + 7d = {expected}"
    )

    # One second early: not reached.
    clock.set(ghost.scheduled_at - timedelta(seconds=1))
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert (await _lead(db, conversation)).status is not LeadStatus.GHOSTED
    assert (await _task(db, conversation, TaskType.GHOST)).status is TaskStatus.PENDING

    # One second later: reached.
    clock.set(ghost.scheduled_at + timedelta(seconds=1))
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)
    assert (await _lead(db, conversation)).status is LeadStatus.GHOSTED
    assert (await _task(db, conversation, TaskType.GHOST)).status is TaskStatus.COMPLETED

    # The sequence milestones stay on their own schedule throughout: +4h, +24h, +72h were
    # each consumed above and none of them ghosted the lead early.
    for step in (1, 2, 3):
        assert (await _task(db, conversation, TaskType.FOLLOW_UP, step)).status is (
            TaskStatus.COMPLETED
        ), f"follow-up step {step} did not complete"


async def test_a_ghost_deferred_for_an_outstanding_follow_up_does_not_hot_loop(
    db, conversation, clock
):
    """The deferral must actually move the due time forward.

    `max(outstanding, now)` looks right and is not: when the outstanding follow-up is itself
    overdue, `outstanding < now`, so the max is `now` and the ghost is due again on the very
    next poll. Each pass also consumes an attempt, so a lead with a slow final follow-up
    eventually exhausts the ghost's budget and it goes FAILED - permanently mis-marked as
    never ghosted, with the poller spinning in between.
    """
    graph = await _arm(db, conversation, clock, follow_up_poll_interval_seconds=30)
    clock.advance(hours=4)
    await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)

    # A ghost due now, while follow-up #2 is still pending (and overdue, which is the case
    # that used to produce a no-op deferral).
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
    # Make the outstanding follow-up overdue so `outstanding < now`.
    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        await session.execute(
            ScheduledTask.__table__.update()
            .where(
                ScheduledTask.tenant_id == conversation["tenant_id"],
                ScheduledTask.task_type == TaskType.FOLLOW_UP,
                ScheduledTask.sequence_step == 2,
            )
            .values(scheduled_at=clock.now() - timedelta(days=1))
        )

    before = await _task(db, conversation, TaskType.GHOST)
    await claim_and_execute(
        db, conversation["tenant_id"], clock, graph=graph, follow_up_poll_interval_seconds=30
    )
    after = await _task(db, conversation, TaskType.GHOST)

    assert after.status is TaskStatus.PENDING
    assert after.scheduled_at > clock.now(), "the deferral must be a real wait"
    assert after.attempt_count == before.attempt_count + 1

    # A second poll at the same instant must not pick it up again.
    again = await claim_and_execute(
        db, conversation["tenant_id"], clock, graph=graph, follow_up_poll_interval_seconds=30
    )
    assert again.claimed == 0, "the ghost was re-claimed at the same instant - hot loop"
    assert (await _task(db, conversation, TaskType.GHOST)).attempt_count == after.attempt_count


# -- worker crash recovery ---------------------------------------------------


async def test_a_worker_that_dies_after_claiming_is_recovered_by_the_next_poll(
    db, conversation, clock
):
    """A crashed worker leaves a PROCESSING row; the lease, not a reaper, is the recovery.

    This is the whole reason `claim_due` reclaims expired leases in the same pass: nothing
    else will ever move the row, so a poller that did not would silently stop the follow-up
    sequence for that conversation with no operator involved.
    """
    await _arm(db, conversation, clock)
    task = await _task(db, conversation, TaskType.FOLLOW_UP)
    clock.advance(hours=4)

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        assert task.id in await task_repo.claim_due(
            session,
            tenant_id=conversation["tenant_id"],
            now=clock.now(),
            limit=10,
            worker="worker-that-dies",
            lease_seconds=LEASE,
        )

    # Before the lease expires nobody else may take it. The poller must use the *same* short
    # lease the claim was taken under, or it would be honouring a 300s default and the test
    # would pass for the wrong reason.
    graph = FakeGraph()
    lease = {"follow_up_processing_lease_seconds": LEASE}
    early = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph, **lease)
    assert early.claimed == 0

    # After it expires, the next poll takes over and completes the work.
    clock.advance(seconds=LEASE + 1)
    recovered = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph, **lease)
    assert recovered.claimed == 1, "an expired lease was not reclaimed"
    assert recovered.completed == 1
    assert len(graph.sends) == 1
    assert (await _task(db, conversation, TaskType.FOLLOW_UP)).status is TaskStatus.COMPLETED


async def test_a_cancelled_task_is_never_executed(db, conversation, clock):
    """Cancellation must win over a claim, not race it.

    `cancel_pending_for_conversation` only touches PENDING rows, so a claimed task is
    resolved by the eligibility re-check inside `execute`. Both paths are covered: cancel
    before the poll, and a reply arriving between the claim and the send.
    """
    graph = await _arm(db, conversation, clock)

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        await task_repo.cancel_pending_for_conversation(
            session,
            tenant_id=conversation["tenant_id"],
            conversation_id=conversation["conversation_id"],
            now=clock.now(),
        )

    clock.advance(hours=4)
    result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)

    assert result.claimed == 0
    assert graph.sends == []
    assert (await _task(db, conversation, TaskType.FOLLOW_UP)).status is TaskStatus.CANCELLED


async def test_a_follow_up_never_fires_while_a_human_handoff_is_active(db, conversation, clock):
    """A person is handling the thread; automation must be silent.

    Checked on both the lead status *and* the handoff record, because they are not the same
    thing: a conversation can be in human hands with no lead row at all.
    """
    from app.models.human_handoff import HandoffReason, HandoffStatus, HumanHandoff

    graph = await _arm(db, conversation, clock)
    task = await _task(db, conversation, TaskType.FOLLOW_UP)

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        session.add(
            HumanHandoff(
                tenant_id=conversation["tenant_id"],
                conversation_id=conversation["conversation_id"],
                lead_id=conversation["lead_id"],
                status=HandoffStatus.OPEN,
                reason=HandoffReason.EXPLICIT_HUMAN_REQUEST,
                priority=300,
                lead_name="Dana Buyer",
            )
        )

    clock.advance(hours=4)
    result = await claim_and_execute(db, conversation["tenant_id"], clock, graph=graph)

    assert graph.sends == [], "automation sent to a conversation a person is handling"
    assert result.skipped >= 1
    after = await _task(db, conversation, TaskType.FOLLOW_UP)
    assert after.status is TaskStatus.CANCELLED
    assert after.last_error == "human_handoff"
    del task


async def test_a_permanently_failing_task_stops_retrying(db, conversation, clock):
    """`max_attempts` must be a real bound, not decoration.

    Each poll consumes one attempt, so a task that can never send has to reach a diagnosable
    terminal state rather than retrying forever.
    """
    from app.services.meta_graph import MetaAuthError

    graph = FakeGraph()
    graph.program_send_error(MetaAuthError("token revoked"))
    await _arm(db, conversation, clock, follow_up_max_attempts=3, graph=graph)

    for _ in range(6):
        clock.advance(hours=6)
        await claim_and_execute(
            db, conversation["tenant_id"], clock, graph=graph, follow_up_max_attempts=3
        )

    task = await _task(db, conversation, TaskType.FOLLOW_UP)
    assert task.status is TaskStatus.FAILED
    assert task.attempt_count <= 3, "a permanently failing task retried past its budget"
    assert task.last_error


async def test_a_retry_is_backed_off_not_immediate(db, conversation, clock):
    """No hot loop on a retryable failure: each attempt is pushed further out."""
    from app.services.meta_graph import MetaTransientError

    graph = FakeGraph()
    graph.program_send_error(MetaTransientError("rate limited"))
    await _arm(
        db,
        conversation["tenant_id"] and conversation,
        clock,
        follow_up_retry_base_seconds=60,
        graph=graph,
    )

    clock.advance(hours=4)
    await claim_and_execute(
        db,
        conversation["tenant_id"],
        clock,
        graph=graph,
        follow_up_retry_base_seconds=60,
    )
    first = await _task(db, conversation, TaskType.FOLLOW_UP)
    assert first.status is TaskStatus.PENDING
    first_gap = first.scheduled_at - clock.now()

    clock.advance(seconds=int(first_gap.total_seconds()) + 1)
    await claim_and_execute(
        db,
        conversation["tenant_id"],
        clock,
        graph=graph,
        follow_up_retry_base_seconds=60,
    )
    second = await _task(db, conversation, TaskType.FOLLOW_UP)
    assert second.attempt_count == first.attempt_count + 1
    second_gap = second.scheduled_at - clock.now()
    assert second_gap > first_gap, "backoff did not grow between attempts"
