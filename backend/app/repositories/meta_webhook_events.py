"""Durable inbox of Meta webhook events.

Two independent idempotency boundaries live here, and they answer different questions.

**Which delivery is this?** `insert_if_new` - a single `INSERT ... ON CONFLICT DO
NOTHING` against `uq_meta_webhook_events_dedupe` makes the database, not the application,
the final guard. A read-then-write would let two concurrent deliveries of the same Meta
event both insert. A duplicate is a *success*: it ACKs and it does not touch the stored
row, so it can neither reset a terminal event nor overwrite a result with a stale payload.

**Who is processing it?** `claim_next_received` and the ownership-guarded
`mark_processed` / `mark_failed`. `locked_by` is the claim token and `locked_at` is what
makes the claim expire, so a crashed worker's event is recovered rather than stranded and a
worker whose lease lapsed cannot write over the worker that replaced it. The shape matches
`scheduled_tasks` on purpose - a second claim scheme in one schema is one more thing to
reason about.
"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.meta_webhook_event import MetaWebhookEvent, MetaWebhookEventStatus


def _claimable(now: datetime, lease_seconds: int):
    """The rows a worker is allowed to take: RECEIVED, or a PROCESSING claim that lapsed.

    A PROCESSING row with a live lease is excluded, so a slow-but-alive worker is never
    raced. `locked_at IS NULL` counts as lapsed: a PROCESSING row that never recorded a
    claim was written before Phase 10-D, i.e. a worker that died leaving no evidence, and
    assuming it is still running would strand the event forever.
    """
    cutoff = now - timedelta(seconds=lease_seconds)
    return or_(
        MetaWebhookEvent.status == MetaWebhookEventStatus.RECEIVED,
        (MetaWebhookEvent.status == MetaWebhookEventStatus.PROCESSING)
        & or_(
            MetaWebhookEvent.locked_at.is_(None),
            MetaWebhookEvent.locked_at < cutoff,
        ),
    )


async def insert_if_new(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    meta_connection_id: uuid.UUID,
    deduplication_key: str,
    meta_event_id: str | None,
    object_type: str | None,
    event_type: str | None,
    source_identifier: str | None,
    payload: dict[str, Any],
    headers: dict[str, Any],
    received_at: datetime,
) -> tuple[MetaWebhookEvent, bool]:
    """Insert the event unless it was already stored.

    Returns `(event, created)`. On a duplicate the existing row is returned with
    `created=False`, so the caller ACKs instead of raising - a Meta retry is a
    success, not an error.
    """
    statement = (
        insert(MetaWebhookEvent)
        .values(
            tenant_id=tenant_id,
            meta_connection_id=meta_connection_id,
            deduplication_key=deduplication_key,
            meta_event_id=meta_event_id,
            object_type=object_type,
            event_type=event_type,
            source_identifier=source_identifier,
            payload=payload,
            headers=headers,
            status=MetaWebhookEventStatus.RECEIVED,
            received_at=received_at,
        )
        .on_conflict_do_nothing(
            index_elements=["meta_connection_id", "deduplication_key"],
        )
        .returning(MetaWebhookEvent.id)
    )
    inserted_id = (await session.execute(statement)).scalar_one_or_none()
    if inserted_id is not None:
        return await get(session, inserted_id), True

    existing = await session.execute(
        select(MetaWebhookEvent).where(
            MetaWebhookEvent.meta_connection_id == meta_connection_id,
            MetaWebhookEvent.deduplication_key == deduplication_key,
        )
    )
    return existing.scalar_one(), False


async def get(session: AsyncSession, event_id: uuid.UUID) -> MetaWebhookEvent | None:
    result = await session.execute(select(MetaWebhookEvent).where(MetaWebhookEvent.id == event_id))
    return result.scalar_one_or_none()


async def get_by_deduplication_key(
    session: AsyncSession, meta_connection_id: uuid.UUID, deduplication_key: str
) -> MetaWebhookEvent | None:
    result = await session.execute(
        select(MetaWebhookEvent).where(
            MetaWebhookEvent.meta_connection_id == meta_connection_id,
            MetaWebhookEvent.deduplication_key == deduplication_key,
        )
    )
    return result.scalar_one_or_none()


async def count_for_connection(session: AsyncSession, meta_connection_id: uuid.UUID) -> int:
    result = await session.execute(
        select(MetaWebhookEvent).where(MetaWebhookEvent.meta_connection_id == meta_connection_id)
    )
    return len(result.scalars().all())


# --- processing seam ---------------------------------------------------------
# A worker runs one short transaction per step, so no database lock is ever held
# across the (future) external calls a processor makes.


async def claim_next_received(
    session: AsyncSession,
    *,
    worker: str,
    now: datetime,
    lease_seconds: int,
) -> MetaWebhookEvent | None:
    """`FOR UPDATE SKIP LOCKED` then RECEIVED -> PROCESSING, in one short transaction.

    The claim is recorded, not merely implied by the status: `locked_by` is the token every
    terminal write is conditional on, and `locked_at` is what lets another worker recover
    this event if this process dies. `attempt_count` is incremented here, so the retry
    budget counts attempts actually made rather than requeues requested.

    Requires `app.current_tenant`, so a worker processes one tenant at a time and can never
    claim another tenant's events.
    """
    result = await session.execute(
        select(MetaWebhookEvent)
        .where(_claimable(now, lease_seconds))
        .order_by(MetaWebhookEvent.received_at, MetaWebhookEvent.id)
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    event = result.scalar_one_or_none()
    if event is None:
        return None
    event.status = MetaWebhookEventStatus.PROCESSING
    event.locked_by = worker
    event.locked_at = now
    event.attempt_count = (event.attempt_count or 0) + 1
    await session.flush()
    return event


async def renew_lease(
    session: AsyncSession, event_id: uuid.UUID, *, worker: str, now: datetime
) -> bool:
    """Extend a live claim so a slow-but-alive worker is not mistaken for a dead one.

    No lease length is taken, for the same reason as `scheduled_tasks.renew_lease`: the
    reclaim query uses the one configured duration, so a per-call value would suggest a
    control that does not exist.
    """
    result = await session.execute(
        update(MetaWebhookEvent)
        .where(
            MetaWebhookEvent.id == event_id,
            MetaWebhookEvent.locked_by == worker,
            MetaWebhookEvent.status == MetaWebhookEventStatus.PROCESSING,
        )
        .values(locked_at=now)
    )
    return bool(result.rowcount)


async def mark_processed(
    session: AsyncSession, event_id: uuid.UUID, *, worker: str, note: str | None = None
) -> bool:
    """Record success, only if `worker` still holds the claim.

    `note` says *why* an event needed no work, e.g. an event type we deliberately ignore, so
    "processed" is distinguishable from "silently dropped".

    Returns whether the write happened. A `False` is not an error to retry: it means this
    worker's lease was reclaimed and someone else owns the outcome now. Swallowing that
    would let a stale worker overwrite a newer worker's terminal state - turning a
    delivered lead into a failure that retries forever, or marking an event processed whose
    business effect had failed.
    """
    result = await session.execute(
        update(MetaWebhookEvent)
        .where(
            MetaWebhookEvent.id == event_id,
            MetaWebhookEvent.locked_by == worker,
            MetaWebhookEvent.status == MetaWebhookEventStatus.PROCESSING,
        )
        .values(
            status=MetaWebhookEventStatus.PROCESSED,
            processed_at=datetime.now(UTC),
            error_message=note,
            locked_by=None,
            locked_at=None,
        )
    )
    return bool(result.rowcount)


async def mark_failed(
    session: AsyncSession, event_id: uuid.UUID, *, worker: str, error_message: str
) -> bool:
    """Record failure, only if `worker` still holds the claim. See `mark_processed`."""
    result = await session.execute(
        update(MetaWebhookEvent)
        .where(
            MetaWebhookEvent.id == event_id,
            MetaWebhookEvent.locked_by == worker,
            MetaWebhookEvent.status == MetaWebhookEventStatus.PROCESSING,
        )
        .values(
            status=MetaWebhookEventStatus.FAILED,
            processed_at=datetime.now(UTC),
            error_message=error_message[:2000],
            locked_by=None,
            locked_at=None,
        )
    )
    return bool(result.rowcount)


# A failure whose category is a permanent one will never succeed on a second attempt - a
# revoked token or an unusable payload. Retrying those would spin a worker forever, so
# requeue refuses them and a human has to look instead.
PERMANENT_FAILURE_PREFIX = "permanent:"
RETRYABLE_FAILURE_PREFIX = "meta_api_unavailable:"


async def requeue_failed(session: AsyncSession, event_id: uuid.UUID, *, max_attempts: int) -> bool:
    """Put a retryable FAILED event back into RECEIVED so a worker claims it again.

    Two independent refusals, both required:

      * the failure must be classified retryable, from the `meta_api_unavailable:` prefix.
        A revoked token or an unusable payload will never succeed on a second attempt, and
        retrying those would spin a worker forever.
      * the event must still have budget. `attempt_count` is incremented at *claim* time,
        so it counts attempts actually made. Without this an event that fails for a
        transient-but-persistent reason (a leadgen id Meta has not finished indexing) would
        be requeued forever.

    The attempt count is deliberately not reset here. It is not consumed by a requeue, so
    resetting it would make the budget a counter of requeues rather than of attempts, and an
    operator looping requeue by hand would clear their own limit.

    Safe to run repeatedly on the same event: each call moves FAILED -> RECEIVED, and a
    second call finds no FAILED row to move.

    Returns whether the event was actually requeued.
    """
    result = await session.execute(
        update(MetaWebhookEvent)
        .where(
            MetaWebhookEvent.id == event_id,
            MetaWebhookEvent.status == MetaWebhookEventStatus.FAILED,
            MetaWebhookEvent.error_message.startswith(RETRYABLE_FAILURE_PREFIX),
            MetaWebhookEvent.attempt_count < max_attempts,
        )
        .values(
            status=MetaWebhookEventStatus.RECEIVED,
            processed_at=None,
        )
    )
    return result.rowcount == 1


__all__ = [
    "MetaWebhookEventStatus",
    "PERMANENT_FAILURE_PREFIX",
    "RETRYABLE_FAILURE_PREFIX",
    "claim_next_received",
    "count_for_connection",
    "get",
    "get_by_deduplication_key",
    "insert_if_new",
    "mark_failed",
    "mark_processed",
    "requeue_failed",
    "renew_lease",
]
