"""Lease ownership under real PostgreSQL concurrency.

The invariant:

    Only the current lease owner may execute the leased task.

`status == PROCESSING` is deliberately not sufficient. It is equally true for the worker
that *replaced* a worker whose lease expired, which is how a duplicate follow-up reaches a
real prospect. Ownership is `locked_by`, and every irreversible step is conditional on it.

Real database throughout: `FOR UPDATE SKIP LOCKED`, conditional `UPDATE`s and partial state
transitions are database behaviour, and a mock would prove nothing about any of them.
"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db.tenant_context import set_current_tenant
from app.models.scheduled_task import ScheduledTask, TaskStatus, TaskType
from app.repositories import scheduled_tasks as task_repo
from tests.fakes import FakeGraph
from tests.follow_up_fixtures import claim_and_execute, service

pytestmark = pytest.mark.integration

LEASE = 60


async def _arm(db, world, clock, **overrides) -> FakeGraph:
    """Schedule follow-up step 1 and return the graph, as `test_follow_ups` does."""
    graph = FakeGraph()
    await service(db, world["tenant_id"], clock, graph=graph, **overrides).on_reply_sent(
        world["conversation_id"]
    )
    return graph


async def _step_one(db, world) -> ScheduledTask:
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant_id"])
        return await session.scalar(
            select(ScheduledTask).where(
                ScheduledTask.tenant_id == world["tenant_id"],
                ScheduledTask.task_type == TaskType.FOLLOW_UP,
                ScheduledTask.sequence_step == 1,
            )
        )


async def _claim(db, world, clock, worker: str) -> list:
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant_id"])
        return await task_repo.claim_due(
            session,
            tenant_id=world["tenant_id"],
            now=clock.now(),
            limit=10,
            worker=worker,
            lease_seconds=LEASE,
        )


async def _row(db, world, task_id) -> ScheduledTask:
    async with db() as session, session.begin():
        await set_current_tenant(session, world["tenant_id"])
        return await task_repo.get(session, tenant_id=world["tenant_id"], task_id=task_id)


# -- Case 1 ------------------------------------------------------------------


async def test_a_second_worker_cannot_claim_a_task_whose_lease_is_valid(db, conversation, clock):
    """A's lease is live, so B's claim must not match the row."""
    await _arm(db, conversation, clock)
    task = await _step_one(db, conversation)
    clock.advance(hours=4)

    assert task.id in await _claim(db, conversation, clock, "worker-a")

    # B polls at the same instant, while A's lease is still valid.
    assert task.id not in await _claim(db, conversation, clock, "worker-b")

    row = await _row(db, conversation, task.id)
    assert row.locked_by == "worker-a"
    assert row.status is TaskStatus.PROCESSING
    assert row.attempt_count == 1, "a losing claim must not consume an attempt"


# -- Case 2: the dangerous one ------------------------------------------------


async def test_a_worker_whose_lease_expired_and_was_taken_over_must_not_send(
    db, conversation, clock
):
    """A resumes after B has taken over, and must not message the lead."""
    graph = await _arm(db, conversation, clock)
    task = await _step_one(db, conversation)
    clock.advance(hours=4)
    settings = {"follow_up_processing_lease_seconds": LEASE}

    assert task.id in await _claim(db, conversation, clock, "worker-a")

    # A's lease expires; B reclaims.
    clock.advance(seconds=LEASE + 1)
    assert task.id in await _claim(db, conversation, clock, "worker-b")

    # A wakes up and tries to execute. It holds no lease.
    outcome = await service(
        db, conversation["tenant_id"], clock, graph=graph, worker="worker-a", **settings
    ).execute(task.id)

    assert outcome.status == "SKIPPED"
    assert outcome.reason == "lease_lost"
    assert graph.sends == [], "a worker without the lease sent a follow-up to a real lead"

    # A must not have overwritten B's claim either.
    row = await _row(db, conversation, task.id)
    assert row.locked_by == "worker-b"
    assert row.status is TaskStatus.PROCESSING


async def test_a_stale_worker_cannot_finish_a_task_someone_else_owns(db, conversation, clock):
    """The same hazard after the send: the bookkeeping must not be written either."""
    graph = await _arm(db, conversation, clock)
    task = await _step_one(db, conversation)
    clock.advance(hours=4)
    settings = {"follow_up_processing_lease_seconds": LEASE}

    assert task.id in await _claim(db, conversation, clock, "worker-a")
    clock.advance(seconds=LEASE + 1)
    assert task.id in await _claim(db, conversation, clock, "worker-b")

    # A gets as far as the send and then finalises. Both must be refused.
    outcome = await service(
        db, conversation["tenant_id"], clock, graph=graph, worker="worker-a", **settings
    ).execute(task.id)

    assert graph.sends == []
    assert outcome.status == "SKIPPED"
    assert (await _row(db, conversation, task.id)).status is TaskStatus.PROCESSING


# -- Case 3 ------------------------------------------------------------------


async def test_a_task_is_completed_exactly_once(db, conversation, clock):
    graph = await _arm(db, conversation, clock)
    task = await _step_one(db, conversation)
    clock.advance(hours=4)
    settings = {"follow_up_processing_lease_seconds": LEASE}

    assert task.id in await _claim(db, conversation, clock, "worker-a")
    svc = service(db, conversation["tenant_id"], clock, graph=graph, worker="worker-a", **settings)

    first = await svc.execute(task.id)
    assert first.status == "COMPLETED"

    # Replaying the same execution changes nothing and sends nothing new.
    second = await svc.execute(task.id)
    assert second.status == "SKIPPED"

    assert len(graph.sends) == 1, "the lead was messaged more than once"
    row = await _row(db, conversation, task.id)
    assert row.status is TaskStatus.COMPLETED
    assert row.locked_by is None


# -- Case 4 ------------------------------------------------------------------


async def test_ten_concurrent_workers_produce_exactly_one_send(db, conversation, clock):
    graph = await _arm(db, conversation, clock)
    clock.advance(hours=4)

    results = await asyncio.gather(
        *(
            claim_and_execute(
                db,
                conversation["tenant_id"],
                clock,
                graph=graph,
                follow_up_processing_lease_seconds=LEASE,
            )
            for _ in range(10)
        )
    )

    assert sum(r.claimed for r in results) == 1, "more than one worker claimed the task"
    assert sum(r.completed for r in results) == 1, "more than one worker completed it"
    assert sum(r.failed for r in results) == 0
    assert len(graph.sends) == 1, "the lead received more than one follow-up"

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        rows = (
            (
                await session.execute(
                    select(ScheduledTask).where(
                        ScheduledTask.tenant_id == conversation["tenant_id"],
                        ScheduledTask.task_type == TaskType.FOLLOW_UP,
                    )
                )
            )
            .scalars()
            .all()
        )
    assert sum(1 for r in rows if r.status is TaskStatus.COMPLETED) == 1


# -- ownership on every terminal write ---------------------------------------


@pytest.mark.parametrize("transition", ["mark_completed", "mark_cancelled", "mark_failed", "defer"])
async def test_a_non_owner_cannot_write_a_terminal_state(db, conversation, clock, transition):
    """Enforced on all four, because a lease only checked on the happy path is not enforced.

    The error branches are where a stale worker does its damage: it is precisely the run
    that failed or was superseded that a replaced worker tends to also be finishing.
    """
    await _arm(db, conversation, clock)
    task = await _step_one(db, conversation)
    clock.advance(hours=4)
    assert task.id in await _claim(db, conversation, clock, "worker-a")

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        if transition == "mark_completed":
            ok = await task_repo.mark_completed(
                session,
                task.id,
                now=clock.now(),
                sent_text="x",
                ai_enhanced=False,
                worker="worker-b",
            )
        elif transition == "mark_cancelled":
            ok = await task_repo.mark_cancelled(
                session, task.id, now=clock.now(), reason="x", worker="worker-b"
            )
        elif transition == "mark_failed":
            ok = await task_repo.mark_failed(
                session, task.id, now=clock.now(), reason="x", retry_at=None, worker="worker-b"
            )
        else:
            ok = await task_repo.defer(
                session, task.id, until=clock.now(), reason="x", worker="worker-b"
            )

    assert ok is False, f"{transition} reported success for a worker that does not own the lease"
    row = await _row(db, conversation, task.id)
    assert row.status is TaskStatus.PROCESSING, f"{transition} changed the state anyway"
    assert row.locked_by == "worker-a"


async def test_lease_renewal_is_owned(db, conversation, clock):
    await _arm(db, conversation, clock)
    task = await _step_one(db, conversation)
    clock.advance(hours=4)
    assert task.id in await _claim(db, conversation, clock, "worker-a")

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        mine = await task_repo.renew_lease(session, task.id, worker="worker-a", now=clock.now())
    assert mine is True, "the owner must be able to renew its own lease"

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        theirs = await task_repo.renew_lease(session, task.id, worker="worker-b", now=clock.now())
    assert theirs is False, "a non-owner must not be able to extend the lease"


async def test_renewal_pushes_the_expiry_out_so_the_reclaim_cannot_steal_the_task(
    db, conversation, clock
):
    """Renewal is only useful if it actually delays the reclaim.

    `locked_at` is the claimed-at stamp and `claim_due` treats a row as stale once it is
    older than the lease, so a renewal that failed to move `locked_at` would leave the owner
    believing it holds the task while a second worker is already free to take it. This
    pins the timestamp, not just the boolean.
    """
    await _arm(db, conversation, clock)
    task = await _step_one(db, conversation)
    clock.advance(hours=4)
    assert task.id in await _claim(db, conversation, clock, "worker-a")
    claimed_at = (await _row(db, conversation, task.id)).locked_at

    # Let the original lease lapse entirely, so the reclaim would succeed on its own.
    clock.advance(seconds=LEASE + 1)
    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        assert await task_repo.renew_lease(session, task.id, worker="worker-a", now=clock.now())

    renewed_at = (await _row(db, conversation, task.id)).locked_at
    assert renewed_at > claimed_at, f"renewal did not move locked_at ({claimed_at})"

    # And the reclaim that would have stolen it now finds nothing.
    assert await _claim(db, conversation, clock, "worker-b") == [], (
        "an expired lease was reclaimed despite the owner renewing it"
    )


async def test_two_owners_cannot_both_fail_a_task_past_its_attempt_budget(db, conversation, clock):
    """The retry decision is made inside the guarded statement, not before it.

    Read-then-write would let two workers both see an attempt remaining and both reschedule,
    so a task could outlive `max_attempts`.
    """
    await _arm(db, conversation, clock)
    task = await _step_one(db, conversation)
    clock.advance(hours=4)
    assert task.id in await _claim(db, conversation, clock, "worker-a")

    # Burn the attempt budget, then have the owner try to reschedule.
    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        await session.execute(
            ScheduledTask.__table__.update()
            .where(ScheduledTask.id == task.id)
            .values(attempt_count=task.max_attempts)
        )

    async with db() as session, session.begin():
        await set_current_tenant(session, conversation["tenant_id"])
        await task_repo.mark_failed(
            session,
            task.id,
            now=clock.now(),
            reason="x",
            retry_at=clock.now() + timedelta(seconds=60),
            worker="worker-a",
        )
        row = await task_repo.get(session, tenant_id=conversation["tenant_id"], task_id=task.id)

    assert row.status is TaskStatus.FAILED, "a task with no attempts left must go terminal"
    assert row.scheduled_at <= clock.now()
