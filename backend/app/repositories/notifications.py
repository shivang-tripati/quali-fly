"""Notification persistence. Create is idempotent; reads are for the inbox.

The whole point of this repository is `create`: it is the only writer, it is keyed on
`(handoff_id, type)`, and it uses `ON CONFLICT DO NOTHING` rather than checking first. A
duplicated webhook reaches it more than once, and a read-then-write would let two of those
create two identical inbox rows for one real escalation.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.internal_notification import (
    InternalNotification,
    NotificationStatus,
    NotificationType,
)


async def create(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    handoff_id: uuid.UUID,
    conversation_id: uuid.UUID,
    lead_id: uuid.UUID | None,
    type: NotificationType,
    title: str,
    message: str,
    payload: dict[str, Any],
    now: datetime,
) -> tuple[InternalNotification, bool]:
    """Create a notification unless one of this type already exists for the handoff.

    Returns `(notification, created)`, and `created` is the only thing a caller should act
    on. Idempotency here is what makes "escalate once, notify once" survive a retried
    webhook, and it is why the acknowledgement and the notification cannot drift apart:
    both are driven off the same "did I create this?" answer.
    """
    statement = (
        insert(InternalNotification)
        .values(
            tenant_id=tenant_id,
            handoff_id=handoff_id,
            conversation_id=conversation_id,
            lead_id=lead_id,
            type=type,
            title=title[:200],
            message=message[:500],
            payload=payload,
            status=NotificationStatus.UNREAD,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(constraint="uq_internal_notifications_handoff_type")
        .returning(InternalNotification.id)
    )
    created_id = (await session.execute(statement)).scalar_one_or_none()
    if created_id is not None:
        return await session.get(InternalNotification, created_id), True  # type: ignore[return-value]
    existing = await session.scalar(
        select(InternalNotification).where(
            InternalNotification.tenant_id == tenant_id,
            InternalNotification.handoff_id == handoff_id,
            InternalNotification.type == type,
        )
    )
    if existing is None:  # pragma: no cover - only if the row vanished mid-transaction
        raise RuntimeError("notification conflict but no row is visible")
    return existing, False


async def get(
    session: AsyncSession, *, tenant_id: uuid.UUID, notification_id: uuid.UUID
) -> InternalNotification | None:
    return await session.scalar(
        select(InternalNotification).where(
            InternalNotification.tenant_id == tenant_id,
            InternalNotification.id == notification_id,
        )
    )


async def list_notifications(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: NotificationStatus | None = None,
    handoff_id: uuid.UUID | None = None,
    limit: int = 50,
) -> list[InternalNotification]:
    """The inbox. Newest first, which is the only order a notification feed is read in."""
    conditions = [InternalNotification.tenant_id == tenant_id]
    if status is not None:
        conditions.append(InternalNotification.status == status)
    if handoff_id is not None:
        conditions.append(InternalNotification.handoff_id == handoff_id)
    rows = (
        (
            await session.execute(
                select(InternalNotification)
                .where(*conditions)
                .order_by(InternalNotification.created_at.desc(), InternalNotification.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def mark_read(
    session: AsyncSession, *, tenant_id: uuid.UUID, notification_id: uuid.UUID, now: datetime
) -> bool:
    """Mark one notification read. Returns whether this call was the one that did it.

    `UNREAD` in the predicate makes a second call a no-op rather than an error, so a double
    click cannot produce a 409 for something the user already asked for.
    """
    result = await session.execute(
        update(InternalNotification)
        .where(
            InternalNotification.tenant_id == tenant_id,
            InternalNotification.id == notification_id,
            InternalNotification.status == NotificationStatus.UNREAD,
        )
        .values(status=NotificationStatus.READ, read_at=now, updated_at=now)
    )
    return bool(result.rowcount)


__all__ = ["create", "get", "list_notifications", "mark_read"]
