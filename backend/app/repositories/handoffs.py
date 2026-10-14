"""Durable handoff persistence: create-or-get, claim, assign, start, resolve.

Two functions carry the real weight, and both are conditional `UPDATE`s rather than
read-then-write, because the same actor reaches them more than once.

`create_active`
    `ON CONFLICT ... DO NOTHING` against the *partial* unique index
    `uq_human_handoffs_active_conversation`. SQLAlchemy has to be told the index predicate
    as well as the columns, or Postgres cannot infer which index to conflict on and the
    statement fails at runtime rather than at import. On conflict the caller reads the
    winner's row back. Ten workers creating a handoff for one event produce one row.

`claim`
    `UPDATE ... WHERE status = 'OPEN' AND assigned_user_id IS NULL RETURNING *`. The
    conditions *are* the concurrency guarantee: two agents claiming the same unassigned
    handoff run two updates, and the row matches the predicate for exactly one of them
    because the first one to commit changes `assigned_user_id`. The loser gets
    `rowcount == 0` and reads the handoff back to discover who won. There is no lock to
    hold and no window between "is it free" and "take it".

Every function is tenant-scoped twice over - an explicit `tenant_id` predicate *and* RLS -
for the same reason Phases 6 and 7 do it: a missing predicate should be caught by reading
the code, not discovered in production.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.human_handoff import (
    ACTIVE_HANDOFF_PREDICATE,
    ACTIVE_HANDOFF_STATUSES,
    HandoffReason,
    HandoffStatus,
    HumanHandoff,
)

#: Statuses a human action may start from. Terminal states are excluded, which is how
#: "RESOLVED -> IN_PROGRESS" becomes impossible rather than merely discouraged.
_MUTABLE = (
    HandoffStatus.OPEN,
    HandoffStatus.ASSIGNED,
    HandoffStatus.IN_PROGRESS,
)


async def get(
    session: AsyncSession, *, tenant_id: uuid.UUID, handoff_id: uuid.UUID
) -> HumanHandoff | None:
    return await session.scalar(
        select(HumanHandoff).where(
            HumanHandoff.tenant_id == tenant_id,
            HumanHandoff.id == handoff_id,
        )
    )


async def get_active(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> HumanHandoff | None:
    """The conversation's active handoff, if it has one. The idempotency read."""
    return await session.scalar(
        select(HumanHandoff).where(
            HumanHandoff.tenant_id == tenant_id,
            HumanHandoff.conversation_id == conversation_id,
            HumanHandoff.status.in_(ACTIVE_HANDOFF_STATUSES),
        )
    )


async def has_active(
    session: AsyncSession, *, tenant_id: uuid.UUID, conversation_id: uuid.UUID
) -> bool:
    """Existence check for the two hot suppression paths.

    Deliberately `exists()` and not a `SELECT` of the row: follow-up eligibility and AI
    suppression both ask this on every claim, and neither needs the handoff's contents -
    only whether automation must stand down. Fetching a row to discard it would be the
    difference between an index-only existence probe and a heap read, on the hottest path
    in the system.
    """
    found = await session.scalar(
        select(func.count())
        .select_from(HumanHandoff)
        .where(
            HumanHandoff.tenant_id == tenant_id,
            HumanHandoff.conversation_id == conversation_id,
            HumanHandoff.status.in_(ACTIVE_HANDOFF_STATUSES),
        )
    )
    return bool(found)


async def create_active(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    lead_id: uuid.UUID | None,
    reason: HandoffReason,
    priority: int,
    detail: str | None,
    context: dict[str, Any],
    now: datetime,
) -> tuple[HumanHandoff, bool]:
    """Create the conversation's active handoff, or return the one that already exists.

    Returns `(handoff, created)`. `created` is not a convenience: the caller uses it to
    decide whether to acknowledge the lead and notify anyone. Only the thread that created
    the handoff may do either, and that is the whole reason a duplicated webhook does not
    produce five acknowledgements.
    """
    statement = (
        insert(HumanHandoff)
        .values(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            lead_id=lead_id,
            status=HandoffStatus.OPEN,
            reason=reason,
            priority=priority,
            detail=detail,
            context=context,
            created_at=now,
            updated_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=[HumanHandoff.conversation_id],
            # `text()` is required, not decorative: SQLAlchemy will not infer that this is
            # SQL rather than a bound expression, and raises at runtime rather than import.
            # The predicate has to match the index's exactly or Postgres cannot infer which
            # index to conflict on.
            index_where=text(ACTIVE_HANDOFF_PREDICATE),
        )
        .returning(HumanHandoff.id)
    )
    created_id = (await session.execute(statement)).scalar_one_or_none()
    if created_id is not None:
        return await session.get(HumanHandoff, created_id), True  # type: ignore[return-value]

    # Lost the race, which is a success from the caller's point of view. The winner's row
    # is the answer to "what is the state of this conversation", and it is committed by now
    # because the unique index forced this transaction to wait on it.
    existing = await get_active(session, tenant_id=tenant_id, conversation_id=conversation_id)
    if existing is None:  # pragma: no cover - only if the winner resolved it meanwhile
        raise RuntimeError(
            "handoff conflict but no active handoff is visible; the active row was resolved "
            "between the conflicting insert and this read"
        )
    return existing, False


async def claim(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    handoff_id: uuid.UUID,
    user_id: uuid.UUID,
    now: datetime,
) -> tuple[HumanHandoff | None, HumanHandoff | None]:
    """Take an unassigned handoff. Returns `(claimed, current)`.

    Exactly one of the two is set. `claimed` is None when someone else got there first, and
    `current` then says who - so a losing agent can be told "already claimed by X" rather
    than the operation silently appearing to succeed.
    """
    row = await session.scalar(
        update(HumanHandoff)
        .where(
            HumanHandoff.tenant_id == tenant_id,
            HumanHandoff.id == handoff_id,
            HumanHandoff.status == HandoffStatus.OPEN,
            HumanHandoff.assigned_user_id.is_(None),
        )
        .values(
            status=HandoffStatus.ASSIGNED,
            assigned_user_id=user_id,
            assigned_at=now,
            updated_at=now,
        )
        .returning(HumanHandoff.id)
    )
    if row is not None:
        return await get(session, tenant_id=tenant_id, handoff_id=row), None
    return None, await get(session, tenant_id=tenant_id, handoff_id=handoff_id)


async def assign(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    handoff_id: uuid.UUID,
    user_id: uuid.UUID,
    now: datetime,
) -> tuple[HumanHandoff | None, HumanHandoff | None]:
    """Put a handoff on a specific agent.

    Unlike `claim` this is not restricted to unassigned rows: an admin reassigning a stuck
    handoff is a legitimate and necessary operation. It is still refused once resolved,
    because a resolved escalation is history.
    """
    row = await session.scalar(
        update(HumanHandoff)
        .where(
            HumanHandoff.tenant_id == tenant_id,
            HumanHandoff.id == handoff_id,
            HumanHandoff.status.in_(_MUTABLE),
        )
        .values(
            status=HandoffStatus.ASSIGNED,
            assigned_user_id=user_id,
            assigned_at=now,
            updated_at=now,
        )
        .returning(HumanHandoff.id)
    )
    if row is not None:
        return await get(session, tenant_id=tenant_id, handoff_id=row), None
    return None, await get(session, tenant_id=tenant_id, handoff_id=handoff_id)


async def start(
    session: AsyncSession, *, tenant_id: uuid.UUID, handoff_id: uuid.UUID, now: datetime
) -> tuple[HumanHandoff | None, HumanHandoff | None]:
    """OPEN|ASSIGNED -> IN_PROGRESS. The agent has picked the work up."""
    row = await session.scalar(
        update(HumanHandoff)
        .where(
            HumanHandoff.tenant_id == tenant_id,
            HumanHandoff.id == handoff_id,
            HumanHandoff.status.in_((HandoffStatus.OPEN, HandoffStatus.ASSIGNED)),
        )
        .values(status=HandoffStatus.IN_PROGRESS, updated_at=now)
        .returning(HumanHandoff.id)
    )
    if row is not None:
        return await get(session, tenant_id=tenant_id, handoff_id=row), None
    return None, await get(session, tenant_id=tenant_id, handoff_id=handoff_id)


async def resolve(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    handoff_id: uuid.UUID,
    note: str | None,
    now: datetime,
) -> tuple[HumanHandoff | None, HumanHandoff | None]:
    """Any non-terminal state -> RESOLVED.

    Deliberately reachable from OPEN as well as from ASSIGNED/IN_PROGRESS. A handoff can be
    resolved without ever being assigned - the lead withdrew, or it was handled in the
    inbox - and forcing it through two pointless transitions to get there would make the
    state machine describe the UI's buttons rather than the work.
    """
    row = await session.scalar(
        update(HumanHandoff)
        .where(
            HumanHandoff.tenant_id == tenant_id,
            HumanHandoff.id == handoff_id,
            HumanHandoff.status.in_(_MUTABLE),
        )
        .values(
            status=HandoffStatus.RESOLVED,
            resolved_at=now,
            resolution_note=note,
            updated_at=now,
        )
        .returning(HumanHandoff.id)
    )
    if row is not None:
        return await get(session, tenant_id=tenant_id, handoff_id=row), None
    return None, await get(session, tenant_id=tenant_id, handoff_id=handoff_id)


async def record_acknowledgement(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    handoff_id: uuid.UUID,
    now: datetime,
    source: str,
) -> None:
    """The lead was successfully told a person is coming.

    `acknowledged_at` is set only on a real delivery. A failed send records
    `acknowledgement_failure` instead, so "we told them" and "we tried" remain
    distinguishable - an operator deciding whether to open the conversation by phone needs
    to tell those apart.
    """
    await session.execute(
        update(HumanHandoff)
        .where(
            HumanHandoff.tenant_id == tenant_id,
            HumanHandoff.id == handoff_id,
        )
        .values(
            acknowledged_at=now,
            acknowledgement_failure=None,
            acknowledgement_source=source,
            updated_at=now,
        )
    )


async def record_acknowledgement_failure(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    handoff_id: uuid.UUID,
    reason: str,
    now: datetime,
) -> None:
    """The acknowledgement did not go out. The handoff itself is untouched and still open.

    That is the important part: the human still has work to do, and a lead who has not been
    told is a lead an agent should probably phone rather than email.
    """
    await session.execute(
        update(HumanHandoff)
        .where(
            HumanHandoff.tenant_id == tenant_id,
            HumanHandoff.id == handoff_id,
        )
        .values(
            acknowledgement_failure=reason[:64],
            updated_at=now,
        )
    )


async def list_handoffs(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    status: HandoffStatus | None = None,
    assigned_user_id: uuid.UUID | None = None,
    reason: HandoffReason | None = None,
    conversation_id: uuid.UUID | None = None,
    lead_id: uuid.UUID | None = None,
    search: str | None = None,
    limit: int = 50,
) -> list[HumanHandoff]:
    """The queue. Highest priority first, then oldest - the order an agent works in.

    `assigned_user_id` is matched exactly, so filtering by it cannot be used to ask "is this
    assigned to nobody?"; callers that want unassigned rows use no filter and read the
    column. The dedicated `?unassigned=true` filter is not added: nothing in the product
    asks for it yet, and a queue endpoint that grows a filter per idea is how a query
    endpoint becomes a report builder.

    `search` matches the denormalised `lead_name`, which is the column the queue is read by
    anyway, so the command palette's "which escalation was about Rahul?" is one indexed-ish
    predicate rather than a join. The wildcards are escaped, because a user typing `%` means
    a percent sign and not "match everything".
    """
    conditions = [HumanHandoff.tenant_id == tenant_id]
    if status is not None:
        conditions.append(HumanHandoff.status == status)
    if assigned_user_id is not None:
        conditions.append(HumanHandoff.assigned_user_id == assigned_user_id)
    if reason is not None:
        conditions.append(HumanHandoff.reason == reason)
    if conversation_id is not None:
        conditions.append(HumanHandoff.conversation_id == conversation_id)
    if lead_id is not None:
        conditions.append(HumanHandoff.lead_id == lead_id)
    if search:
        pattern = (
            "%" + search.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        )
        conditions.append(HumanHandoff.lead_name.ilike(pattern, escape="\\"))

    rows = (
        (
            await session.execute(
                select(HumanHandoff)
                .where(*conditions)
                # DESC on both: a newer high-priority escalation is worth more than an
                # older low-priority one, and within a priority the oldest has been waiting
                # longest. Ties break on id so pagination is stable rather than arbitrary.
                .order_by(
                    HumanHandoff.priority.desc(),
                    HumanHandoff.created_at.asc(),
                    HumanHandoff.id.asc(),
                )
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def count_by_status(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, int]:
    """Queue header counts, so a UI can show "4 open" without listing them all."""
    rows = (
        await session.execute(
            select(HumanHandoff.status, func.count())
            .where(HumanHandoff.tenant_id == tenant_id)
            .group_by(HumanHandoff.status)
        )
    ).all()
    counts = {status.value: 0 for status in HandoffStatus}
    for status, count in rows:
        counts[status.value] = int(count)
    return counts


__all__ = [
    "assign",
    "claim",
    "count_by_status",
    "create_active",
    "get",
    "get_active",
    "has_active",
    "list_handoffs",
    "record_acknowledgement",
    "record_acknowledgement_failure",
    "resolve",
    "start",
]
