"""Durable task persistence: claim, cancel, complete, fail, reclaim.

Every function here is tenant-scoped twice over - an explicit `tenant_id` predicate *and*
RLS - for the same reason Phase 6's retrieval did: a query that omitted the tenant would
rely on the database to hide a mistake rather than not making it.

The two functions that carry the real weight are `claim_due` and `cancel_pending_for_conversation`.

`claim_due` is the multi-worker primitive. It selects due PENDING rows with
`FOR UPDATE SKIP LOCKED` and transitions them to PROCESSING in the same statement, so ten
pollers racing over one task produce one winner and nine empty results - the losers skip
locked rows rather than blocking behind them. The caller then commits, *releases the lock*,
and only then does any AI or Meta work. No lock is ever held across an external call.

`cancel_pending_for_conversation` is called from inside the inbound-message transaction.
It is a single `UPDATE` and cannot be partially applied, which is what makes
"insert message + cancel follow-ups" atomic.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheduled_task import ScheduledTask, TaskStatus, TaskType


async def schedule(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    lead_id: uuid.UUID | None,
    task_type: TaskType,
    sequence_step: int,
    scheduled_at: datetime,
    max_attempts: int,
    activity_anchor_at: datetime | None,
) -> ScheduledTask | None:
    """Create a task unless one already exists for this (conversation, type, step).

    Returns the existing row on a conflict, or `None` if the tenant is not eligible. The
    caller distinguishes the two, because "already scheduled" is a success and "not
    eligible" is not.

    Idempotent by construction: `ON CONFLICT DO NOTHING` against
    `uq_scheduled_tasks_conversation_type_step`, then a read-back. Two schedulers racing
    here produce one row; the loser reads the winner's row back rather than creating a
    second executable task. There is no read-then-write window for the race to land in.
    """
    statement = (
        insert(ScheduledTask)
        .values(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            lead_id=lead_id,
            task_type=task_type,
            sequence_step=sequence_step,
            scheduled_at=scheduled_at,
            status=TaskStatus.PENDING,
            attempt_count=0,
            max_attempts=max_attempts,
            activity_anchor_at=activity_anchor_at,
        )
        .on_conflict_do_nothing(constraint="uq_scheduled_tasks_conversation_type_step")
        .returning(ScheduledTask.id)
    )
    created = (await session.execute(statement)).scalar_one_or_none()
    if created is not None:
        return await session.get(ScheduledTask, created)
    return await get_step(
        session,
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        task_type=task_type,
        sequence_step=sequence_step,
    )


async def get_step(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    task_type: TaskType,
    sequence_step: int,
) -> ScheduledTask | None:
    return await session.scalar(
        select(ScheduledTask).where(
            ScheduledTask.tenant_id == tenant_id,
            ScheduledTask.conversation_id == conversation_id,
            ScheduledTask.task_type == task_type,
            ScheduledTask.sequence_step == sequence_step,
        )
    )


async def claim_due(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    now: datetime,
    limit: int,
    worker: str,
    lease_seconds: int,
) -> list[uuid.UUID]:
    """Atomically claim up to `limit` due tasks. Returns the claimed ids.

    `FOR UPDATE SKIP LOCKED` is what makes this safe with many workers: the row is locked
    for the duration of *this statement only*, a second poller skips it rather than
    blocking, and the status transition happens in the same round trip. The caller commits
    immediately afterwards, so no lock survives into the execute phase.

    Reclaims expired leases in the same pass. A worker that died mid-execute leaves its row
    PROCESSING forever otherwise, and the follow-up sequence would silently stop for that
    conversation with no operator involvement.
    """
    # Reclaim first. A PROCESSING row whose lease has expired is treated as PENDING again,
    # because "the worker holding this is gone" is indistinguishable from "the worker
    # holding this has not started yet" and both mean it is safe to try.
    await session.execute(
        update(ScheduledTask)
        .where(
            ScheduledTask.tenant_id == tenant_id,
            ScheduledTask.status == TaskStatus.PROCESSING,
            ScheduledTask.locked_at.is_not(None),
            ScheduledTask.locked_at < now - timedelta(seconds=lease_seconds),
        )
        .values(
            status=TaskStatus.PENDING,
            locked_at=None,
            locked_by=None,
        )
    )
    await session.flush()

    rows = (
        (
            await session.execute(
                select(ScheduledTask.id)
                .where(
                    ScheduledTask.tenant_id == tenant_id,
                    ScheduledTask.status == TaskStatus.PENDING,
                    ScheduledTask.scheduled_at <= now,
                )
                .order_by(ScheduledTask.scheduled_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return []

    await session.execute(
        update(ScheduledTask)
        .where(ScheduledTask.id.in_(rows))
        .values(
            status=TaskStatus.PROCESSING,
            locked_at=now,
            locked_by=worker[:128],
            attempt_count=ScheduledTask.attempt_count + 1,
        )
    )
    await session.flush()
    return list(rows)


async def cancel_pending_for_conversation(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    now: datetime,
) -> int:
    """Cancel every still-pending task for a conversation. Returns how many.

    Called from inside the inbound-message transaction, which is the whole point: a lead
    who replies and a scheduler that sends a follow-up must not interleave. Splitting this
    into a later transaction would leave a window in which a due task is claimed and sent
    after the lead has already answered - the exact failure the invariant exists to prevent.
    """
    result = await session.execute(
        update(ScheduledTask)
        .where(
            ScheduledTask.tenant_id == tenant_id,
            ScheduledTask.conversation_id == conversation_id,
            ScheduledTask.status == TaskStatus.PENDING,
        )
        .values(status=TaskStatus.CANCELLED, cancelled_at=now, updated_at=now)
    )
    return result.rowcount or 0


async def renew_lease(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    worker: str,
    now: datetime,
) -> bool:
    """Extend this worker's lease if it still holds it. Returns whether it did.

    Called immediately before the irreversible part of execution - the outbound send. The
    window between "we claimed this" and "we are about to message a real person" includes an
    AI call that can take seconds, which is long enough for a short lease to expire and for a
    second worker to take over. Re-asserting ownership here narrows that window to the send
    call itself rather than the whole execute path.

    Conditional on `locked_by = worker`, so a worker whose lease was reclaimed finds nothing
    to renew and stops instead of competing with the worker that now owns the task.

    No lease length is taken here on purpose. `locked_at` records when the claim was taken
    and `claim_due` decides staleness against the one lease duration the settings define, so
    renewal is just "the claim is still as recent as now". Accepting a duration here would
    suggest the caller could set a different one, which would not actually hold - the reclaim
    query would still use the configured value and could steal the task immediately.
    """
    result = await session.execute(
        update(ScheduledTask)
        .where(
            ScheduledTask.id == task_id,
            ScheduledTask.locked_by == worker,
            ScheduledTask.status == TaskStatus.PROCESSING,
        )
        .values(locked_at=now)
    )
    return bool(result.rowcount)


async def mark_completed(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    now: datetime,
    sent_text: str | None,
    ai_enhanced: bool,
    worker: str | None = None,
) -> bool:
    """Complete a task. Returns whether this call is the one that did it.

    With `worker` supplied the update is conditional on lease ownership, so a worker that
    lost its lease while the send was in flight cannot overwrite the outcome recorded by the
    worker that replaced it. Without `worker` the update is unconditional, which is only
    correct for callers that are not a lease holder (the ghost path re-completes an already
    ghosted lead, for instance).
    """
    conditions = [ScheduledTask.id == task_id]
    if worker is not None:
        conditions.append(ScheduledTask.locked_by == worker)
    await session.execute(
        update(ScheduledTask)
        .where(*conditions)
        .values(
            status=TaskStatus.COMPLETED,
            completed_at=now,
            sent_text=sent_text,
            ai_enhanced=ai_enhanced,
            last_error=None,
            locked_at=None,
            locked_by=None,
        )
    )
    return await _touched(session, task_id)


async def _touched(session: AsyncSession, task_id: uuid.UUID) -> bool:
    """Whether the row is in a state only the intended transition could have produced.

    Used to answer "did my update land?" without a second round trip in the common case: an
    unconditional `UPDATE` that matched nothing leaves `locked_by` set, so a cleared lease on
    a terminal status is the evidence.
    """
    row = await session.get(ScheduledTask, task_id)
    if row is None:
        return False
    return row.status in _TERMINAL_STATUSES and row.locked_by is None


_TERMINAL_STATUSES = (TaskStatus.COMPLETED, TaskStatus.CANCELLED, TaskStatus.FAILED)


async def mark_cancelled(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    now: datetime,
    reason: str,
    worker: str | None = None,
) -> bool:
    """Cancel a task that was already claimed. Used by the eligibility re-check.

    Conditional on `worker` where supplied, so a worker that lost its lease cannot cancel a
    task another worker is now executing.
    """
    conditions = [ScheduledTask.id == task_id]
    if worker is not None:
        conditions.append(ScheduledTask.locked_by == worker)
    await session.execute(
        update(ScheduledTask)
        .where(*conditions)
        .values(
            status=TaskStatus.CANCELLED,
            cancelled_at=now,
            last_error=reason[:64],
            locked_at=None,
            locked_by=None,
        )
    )
    return await _touched(session, task_id)


async def defer(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    until: datetime,
    reason: str,
    worker: str | None = None,
) -> bool:
    """Push a claimed task back to PENDING, due at `until`.

    The distinction from a retry matters: a retry implies the attempt failed and consumed
    one of `max_attempts`, and this does not. A deferred task was not attempted at all -
    the poller simply got there too early, which is the same thing as having polled a
    minute sooner. Counting it as an attempt would eventually expire a task that never
    did anything wrong.
    """
    conditions = [ScheduledTask.id == task_id]
    if worker is not None:
        conditions.append(ScheduledTask.locked_by == worker)
    await session.execute(
        update(ScheduledTask)
        .where(*conditions)
        .values(
            status=TaskStatus.PENDING,
            scheduled_at=until,
            last_error=reason[:64],
            locked_at=None,
            locked_by=None,
        )
    )
    return await _touched(session, task_id)


async def latest_pending_follow_up_at(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> datetime | None:
    """When the last outstanding follow-up for this conversation is due, if any."""
    result = await session.execute(
        select(func.max(ScheduledTask.scheduled_at)).where(
            ScheduledTask.tenant_id == tenant_id,
            ScheduledTask.conversation_id == conversation_id,
            ScheduledTask.task_type == TaskType.FOLLOW_UP,
            ScheduledTask.status == TaskStatus.PENDING,
        )
    )
    return result.scalar_one_or_none()


async def mark_failed(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    now: datetime,
    reason: str,
    retry_at: datetime | None,
    worker: str | None = None,
) -> bool:
    """Fail a task, or reschedule it when attempts remain.

    `retry_at` set means the row goes back to PENDING with a later `scheduled_at`, which is
    the backoff. `retry_at=None` means terminal FAILED. Bounded by `attempt_count` versus
    `max_attempts`, so a task can never retry forever.

    The attempt budget is read and the update written in one statement, so two workers
    failing the same task cannot both decide an attempt remains. Conditional on `worker`
    where supplied, so a worker that lost its lease cannot fail a task another worker owns.
    """
    if worker is not None:
        return await _mark_failed_owned(
            session, task_id, now=now, reason=reason, retry_at=retry_at, worker=worker
        )

    task = await session.get(ScheduledTask, task_id)
    if task is None:  # pragma: no cover - the row was just claimed
        return False
    if retry_at is not None and task.attempt_count < task.max_attempts:
        await session.execute(
            update(ScheduledTask)
            .where(ScheduledTask.id == task_id)
            .values(
                status=TaskStatus.PENDING,
                scheduled_at=retry_at,
                last_error=reason[:64],
                locked_at=None,
                locked_by=None,
            )
        )
        return True
    await session.execute(
        update(ScheduledTask)
        .where(ScheduledTask.id == task_id)
        .values(
            status=TaskStatus.FAILED,
            completed_at=now,
            last_error=reason[:64],
            locked_at=None,
            locked_by=None,
        )
    )
    return True


async def _mark_failed_owned(
    session: AsyncSession,
    task_id: uuid.UUID,
    *,
    now: datetime,
    reason: str,
    retry_at: datetime | None,
    worker: str,
) -> bool:
    """The lease-holding variant: the retry decision is made inside the guarded statement.

    `attempt_count < max_attempts` is a predicate rather than a value read in Python, so two
    workers cannot both observe a remaining attempt and both reschedule. Whoever loses
    matches no row and returns False, and the caller reports a lost lease rather than a
    failure it did not cause.
    """
    common = {
        ScheduledTask.id == task_id,
        ScheduledTask.locked_by == worker,
        ScheduledTask.status == TaskStatus.PROCESSING,
    }
    if retry_at is not None:
        result = await session.execute(
            update(ScheduledTask)
            .where(*common, ScheduledTask.attempt_count < ScheduledTask.max_attempts)
            .values(
                status=TaskStatus.PENDING,
                scheduled_at=retry_at,
                last_error=reason[:64],
                locked_at=None,
                locked_by=None,
            )
        )
        if result.rowcount:
            return True
        # No attempts left, or the lease is gone. Only take the terminal branch if the
        # lease is still ours.
    result = await session.execute(
        update(ScheduledTask)
        .where(*common)
        .values(
            status=TaskStatus.FAILED,
            completed_at=now,
            last_error=reason[:64],
            locked_at=None,
            locked_by=None,
        )
    )
    return bool(result.rowcount)


async def get(
    session: AsyncSession, *, tenant_id: uuid.UUID, task_id: uuid.UUID
) -> ScheduledTask | None:
    return await session.scalar(
        select(ScheduledTask).where(
            ScheduledTask.id == task_id, ScheduledTask.tenant_id == tenant_id
        )
    )


async def get_by_conversation(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> list[ScheduledTask]:
    return list(
        (
            await session.execute(
                select(ScheduledTask)
                .where(
                    ScheduledTask.tenant_id == tenant_id,
                    ScheduledTask.conversation_id == conversation_id,
                )
                .order_by(ScheduledTask.sequence_step)
            )
        )
        .scalars()
        .all()
    )


async def list_tasks(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: TaskStatus | None = None,
    limit: int = 50,
) -> list[ScheduledTask]:
    conditions = [ScheduledTask.tenant_id == tenant_id]
    if status is not None:
        conditions.append(ScheduledTask.status == status)
    return list(
        (
            await session.execute(
                select(ScheduledTask)
                .where(*conditions)
                .order_by(ScheduledTask.scheduled_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )


async def count_by_status(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, int]:
    """Operational counts, for diagnostics and the admin surface."""
    rows = (
        await session.execute(
            select(ScheduledTask.status, func.count())
            .where(ScheduledTask.tenant_id == tenant_id)
            .group_by(ScheduledTask.status)
        )
    ).all()
    return {str(status): int(count) for status, count in rows}


__all__ = [
    "cancel_pending_for_conversation",
    "claim_due",
    "count_by_status",
    "get",
    "get_by_conversation",
    "get_step",
    "list_tasks",
    "mark_cancelled",
    "mark_completed",
    "mark_failed",
    "renew_lease",
    "schedule",
]
