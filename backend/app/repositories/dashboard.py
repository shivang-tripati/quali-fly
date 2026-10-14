"""Read-only aggregation for the dashboard.

This module exists because the dashboard's central question is *"what needs my attention
right now?"*, and answering it by fetching every lead, every conversation, every task and
every handoff into the browser would be slow, wasteful, and would hand the client more data
than it needs. One aggregated read, computed in the database, is both faster and smaller.

Three rules hold for everything here:

**Read-only.** No function writes. The dashboard observes; it never changes a lead, a task
or a handoff. If a query here ever needed an `UPDATE`, it belongs in the service that owns
that state machine.

**Tenant-scoped twice over** - an explicit `tenant_id` predicate *and* RLS - matching every
repository in Phases 2-8. A dashboard that shows another tenant's numbers is the most
damaging possible leak, and it is the one screen every user sees first.

**Counting is done in SQL, not in Python.** Rows are aggregated and grouped by the database;
nothing here materialises a table to count it in a Python loop. `count_by_status` returns
one row per status regardless of how many leads exist.

The vocabulary here is deliberately the *user's* vocabulary, not the database's: an
`AttentionItem` is a "handoff waiting", not a `human_handoffs` row with `status = 'OPEN'`.
Translating at the boundary means the UI never has to know what a `task_type` is, and a
rename in the database cannot leak into the product's language.
"""

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.ai_message_run import AIMessageRun, AIRunStatus
from app.models.conversation import (
    Channel,
    Conversation,
    Message,
    MessageDelivery,
    MessageDirection,
)
from app.models.human_handoff import (
    ACTIVE_HANDOFF_STATUSES,
    HumanHandoff,
)
from app.models.knowledge_base import KnowledgeBase
from app.models.lead import Lead, LeadStatus
from app.models.meta_connection import MetaConnection, MetaConnectionStatus
from app.models.scheduled_task import ScheduledTask, TaskStatus, TaskType

#: How far ahead "upcoming" follow-ups reach. A day is long enough to plan around and short
#: enough that the list stays a list.
UPCOMING_WINDOW = timedelta(days=1)


@dataclass(frozen=True)
class LeadCounts:
    """Lead totals, plus how many arrived today.

    `new_today` is separate from the per-status counts because "12 new leads" and "12 leads
    in NEW state" are different numbers - the first is about arrival, the second about work
    not yet done - and a dashboard that conflates them reports the wrong one.
    """

    by_status: dict[LeadStatus, int]
    total: int
    new_today: int


@dataclass(frozen=True)
class HandoffCounts:
    open_count: int
    unassigned_count: int
    mine_count: int


@dataclass(frozen=True)
class FollowUpCounts:
    overdue: int
    due_today: int
    upcoming: int
    total_pending: int


@dataclass(frozen=True)
class AttentionHandoff:
    handoff: HumanHandoff
    lead_name: str | None


@dataclass(frozen=True)
class AttentionFollowUp:
    task: ScheduledTask
    lead_name: str | None
    channel: Channel | None


@dataclass(frozen=True)
class AttentionFailure:
    """A processing failure a human can actually act on.

    `subject` is pre-joined so the UI never has to turn an id into a name, and `detail` is
    a category we chose rather than anything a provider said - a provider error can echo the
    lead's own message, and this row is rendered in a list.
    """

    kind: str
    subject: str | None
    detail: str | None
    occurred_at: datetime
    conversation_id: uuid.UUID | None
    handoff_id: uuid.UUID | None


@dataclass(frozen=True)
class RecentConversation:
    conversation: Conversation
    lead_name: str | None
    lead_status: LeadStatus | None
    last_message_text: str | None
    last_message_direction: str | None
    last_message_delivery: str | None
    last_message_sent_at: datetime | None
    has_active_handoff: bool


# -- leads -------------------------------------------------------------------


async def lead_counts(session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime) -> LeadCounts:
    rows = (
        await session.execute(
            select(Lead.status, func.count())
            .where(Lead.tenant_id == tenant_id)
            .group_by(Lead.status)
        )
    ).all()
    by_status = {status: int(count) for status, count in rows}

    total = (
        await session.execute(
            select(func.count()).select_from(Lead).where(Lead.tenant_id == tenant_id)
        )
    ).scalar_one()

    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    new_today = (
        await session.execute(
            select(func.count())
            .select_from(Lead)
            .where(Lead.tenant_id == tenant_id, Lead.created_at >= start_of_day)
        )
    ).scalar_one()

    return LeadCounts(
        by_status=by_status,
        total=int(total),
        new_today=int(new_today),
    )


# -- handoffs ----------------------------------------------------------------


def _active_handoff_base(tenant_id: uuid.UUID) -> Select:
    return select(HumanHandoff).where(
        HumanHandoff.tenant_id == tenant_id,
        HumanHandoff.status.in_(ACTIVE_HANDOFF_STATUSES),
    )


async def handoff_counts(
    session: AsyncSession, *, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> HandoffCounts:
    counts = (
        await session.execute(
            select(HumanHandoff.status, HumanHandoff.assigned_user_id, func.count())
            .where(
                HumanHandoff.tenant_id == tenant_id,
                HumanHandoff.status.in_(ACTIVE_HANDOFF_STATUSES),
            )
            .group_by(HumanHandoff.status, HumanHandoff.assigned_user_id)
        )
    ).all()

    open_count = sum(int(count) for _, _, count in counts)
    unassigned = sum(int(count) for _, assignee, count in counts if assignee is None)
    mine = sum(
        int(count) for _, assignee, count in counts if assignee is not None and assignee == user_id
    )
    return HandoffCounts(open_count=open_count, unassigned_count=unassigned, mine_count=mine)


async def active_handoffs(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int
) -> list[AttentionHandoff]:
    """Open handoffs, highest priority and oldest first - the order a human works in."""
    rows = (
        await session.execute(
            _active_handoff_base(tenant_id)
            .outerjoin(Lead, Lead.id == HumanHandoff.lead_id)
            .add_columns(Lead.name)
            .order_by(
                HumanHandoff.priority.desc(),
                HumanHandoff.created_at.asc(),
                HumanHandoff.id.asc(),
            )
            .limit(limit)
        )
    ).all()
    return [AttentionHandoff(handoff=handoff, lead_name=name) for handoff, name in rows]


# -- follow-ups --------------------------------------------------------------


def _pending_follow_ups(tenant_id: uuid.UUID, now: datetime) -> Select:
    """PENDING follow-up tasks only.

    `GHOST` tasks are excluded deliberately. A ghost task is an internal bookkeeping row for
    a lead who has gone quiet, and showing "1 follow-up due" for a person nobody has heard
    from in a week is exactly the kind of scheduler vocabulary leaking into the product.
    """
    return select(ScheduledTask).where(
        ScheduledTask.tenant_id == tenant_id,
        ScheduledTask.status == TaskStatus.PENDING,
        ScheduledTask.task_type == TaskType.FOLLOW_UP,
        ScheduledTask.scheduled_at <= now + UPCOMING_WINDOW,
    )


async def follow_up_counts(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime
) -> FollowUpCounts:
    end_of_day = now.replace(hour=23, minute=59, second=59, microsecond=999_999)
    rows = (
        await session.execute(
            select(ScheduledTask.scheduled_at, func.count())
            .where(
                ScheduledTask.tenant_id == tenant_id,
                ScheduledTask.status == TaskStatus.PENDING,
                ScheduledTask.task_type == TaskType.FOLLOW_UP,
                ScheduledTask.scheduled_at <= end_of_day,
            )
            .group_by(ScheduledTask.scheduled_at)
        )
    ).all()

    overdue = due_today = upcoming = 0
    for due_at, count in rows:
        if due_at < now:
            overdue += int(count)
        elif due_at <= end_of_day:
            due_today += int(count)
        else:
            upcoming += int(count)

    total_pending = (
        await session.execute(
            select(func.count())
            .select_from(ScheduledTask)
            .where(
                ScheduledTask.tenant_id == tenant_id,
                ScheduledTask.status == TaskStatus.PENDING,
                ScheduledTask.task_type == TaskType.FOLLOW_UP,
            )
        )
    ).scalar_one()

    return FollowUpCounts(
        overdue=overdue,
        due_today=due_today,
        upcoming=upcoming,
        total_pending=int(total_pending),
    )


async def pending_follow_ups(
    session: AsyncSession, *, tenant_id: uuid.UUID, now: datetime, limit: int
) -> list[AttentionFollowUp]:
    rows = (
        await session.execute(
            _pending_follow_ups(tenant_id, now)
            .outerjoin(Lead, Lead.id == ScheduledTask.lead_id)
            .outerjoin(Conversation, Conversation.id == ScheduledTask.conversation_id)
            .add_columns(Lead.name, Conversation.channel)
            .order_by(ScheduledTask.scheduled_at.asc())
            .limit(limit)
        )
    ).all()
    return [
        AttentionFollowUp(task=task, lead_name=name, channel=channel)
        for task, name, channel in rows
    ]


# -- failures ----------------------------------------------------------------


async def recent_failures(
    session: AsyncSession, *, tenant_id: uuid.UUID, since: datetime, limit: int
) -> list[AttentionFailure]:
    """AI runs and outbound sends that failed recently.

    Two kinds in one list because from the user's seat they are the same question - "did
    something break?" - and separating them into two screens would make them check both.

    `error_category` / `failure_reason` only. Never the provider's message, which can echo
    the lead's own words or a prompt.
    """
    failed_runs = (
        await session.execute(
            select(AIMessageRun, Conversation.lead_id)
            .outerjoin(Conversation, Conversation.id == AIMessageRun.conversation_id)
            .where(
                AIMessageRun.tenant_id == tenant_id,
                AIMessageRun.status == AIRunStatus.FAILED,
                AIMessageRun.completed_at >= since,
            )
            .order_by(AIMessageRun.completed_at.desc())
            .limit(limit)
        )
    ).all()

    failed_sends = (
        await session.execute(
            select(Message, Conversation.lead_id)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.tenant_id == tenant_id,
                Message.direction == MessageDirection.OUTBOUND,
                Message.delivery == MessageDelivery.FAILED,
                Message.sent_at >= since,
            )
            .order_by(Message.sent_at.desc())
            .limit(limit)
        )
    ).all()

    lead_names = await _lead_names(
        session,
        tenant_id=tenant_id,
        lead_ids={lead_id for _, lead_id in failed_runs if lead_id is not None}
        | {lead_id for _, lead_id in failed_sends if lead_id is not None},
    )

    failures: list[AttentionFailure] = []
    for run, lead_id in failed_runs:
        failures.append(
            AttentionFailure(
                kind="ai_processing",
                subject=lead_names.get(lead_id),
                detail=run.error_category,
                occurred_at=run.completed_at or run.created_at,
                conversation_id=run.conversation_id,
                handoff_id=None,
            )
        )
    for message, lead_id in failed_sends:
        failures.append(
            AttentionFailure(
                kind="message_not_delivered",
                subject=lead_names.get(lead_id),
                detail=message.failure_reason,
                occurred_at=message.sent_at,
                conversation_id=message.conversation_id,
                handoff_id=None,
            )
        )

    failures.sort(key=lambda f: f.occurred_at, reverse=True)
    return failures[:limit]


async def _lead_names(
    session: AsyncSession, *, tenant_id: uuid.UUID, lead_ids: set[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not lead_ids:
        return {}
    rows = (
        await session.execute(
            select(Lead.id, Lead.name).where(Lead.tenant_id == tenant_id, Lead.id.in_(lead_ids))
        )
    ).all()
    return {lead_id: name for lead_id, name in rows if name}


# -- conversations -----------------------------------------------------------


async def recent_conversations(
    session: AsyncSession, *, tenant_id: uuid.UUID, limit: int
) -> list[RecentConversation]:
    """Most recently active threads, with the context needed to act on one.

    Two statements, not two per row. The last message is fetched with `DISTINCT ON` over
    the already-known conversation ids, which is the one Postgres idiom that expresses
    "the newest row per group" directly; the alternative - a correlated subquery per column,
    or a query per row - would be either unreadable or an N+1 on the screen the user opens
    first, which is the worst place to pay it.
    """
    conversations = (
        await session.execute(
            select(Conversation, Lead.name, Lead.status)
            .outerjoin(Lead, Lead.id == Conversation.lead_id)
            .where(Conversation.tenant_id == tenant_id)
            .order_by(
                Conversation.last_message_at.desc().nulls_last(),
                Conversation.created_at.desc(),
            )
            .limit(limit)
        )
    ).all()
    if not conversations:
        return []

    conversation_ids = [row[0].id for row in conversations]
    latest = (
        await session.execute(
            select(
                Message.conversation_id,
                Message.text,
                Message.direction,
                Message.delivery,
                Message.sent_at,
            )
            .where(
                Message.tenant_id == tenant_id,
                Message.conversation_id.in_(conversation_ids),
            )
            .order_by(Message.conversation_id, Message.sent_at.desc())
            .distinct(Message.conversation_id)
        )
    ).all()
    last_by_conversation = {row[0]: (row[1], row[2], row[3], row[4]) for row in latest}

    open_handoff_rows = (
        await session.execute(
            select(HumanHandoff.conversation_id)
            .where(
                HumanHandoff.tenant_id == tenant_id,
                HumanHandoff.conversation_id.in_(conversation_ids),
                HumanHandoff.status.in_(ACTIVE_HANDOFF_STATUSES),
            )
            .distinct()
        )
    ).all()
    with_handoff = {row[0] for row in open_handoff_rows}

    results: list[RecentConversation] = []
    for conversation, lead_name, lead_status in conversations:
        text, direction, delivery, sent_at = last_by_conversation.get(
            conversation.id, (None, None, None, None)
        )
        results.append(
            RecentConversation(
                conversation=conversation,
                lead_name=lead_name,
                lead_status=lead_status,
                last_message_text=text,
                last_message_direction=direction.value if direction else None,
                last_message_delivery=delivery.value if delivery else None,
                last_message_sent_at=sent_at,
                has_active_handoff=conversation.id in with_handoff,
            )
        )
    return results


# -- integrations ------------------------------------------------------------


async def meta_connection_health(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, Any]:
    """Connection state for the health strip.

    Never the token, never the page's token hash, never a scope list that could be mistaken
    for a credential - just whether a connection exists, whether it is usable, and when we
    last heard from Meta.
    """
    rows = (
        await session.execute(
            select(
                MetaConnection.id,
                MetaConnection.status,
                MetaConnection.display_name,
                MetaConnection.page_id,
                MetaConnection.access_token_encrypted,
                MetaConnection.last_webhook_at,
            )
            .where(MetaConnection.tenant_id == tenant_id)
            .order_by(MetaConnection.created_at.asc())
        )
    ).all()

    connections = [
        {
            "id": connection_id,
            "status": status.value,
            "name": display_name or page_id or "Meta account",
            "configured": bool(token),
            "last_webhook_at": last_webhook_at,
        }
        for connection_id, status, display_name, page_id, token, last_webhook_at in rows
    ]
    active = [c for c in connections if c["status"] == MetaConnectionStatus.ACTIVE.value]
    needs_attention = [c for c in connections if c["status"] != MetaConnectionStatus.ACTIVE.value]
    return {
        "total": len(connections),
        "active": len(active),
        "needs_attention": len(needs_attention),
        "connections": connections,
    }


async def knowledge_counts(session: AsyncSession, *, tenant_id: uuid.UUID) -> dict[str, int]:
    """Documents and chunks.

    `documents` is a `DISTINCT` count of `document_id`, because `knowledge_base` stores one
    row per *chunk* - counting rows there would tell an operator they have 400 documents
    when they have 14. That error is the whole reason this is written out rather than
    borrowed from the list endpoint's `chunk_count`.

    A tenant with zero documents has an AI that knows nothing, which is actionable in a way
    "0 documents" is not, so the dashboard treats it as an attention item rather than a
    statistic.
    """
    row = (
        await session.execute(
            select(
                func.count(func.distinct(KnowledgeBase.document_id)),
                func.count(KnowledgeBase.chunk_index),
            ).where(
                KnowledgeBase.tenant_id == tenant_id,
                KnowledgeBase.is_active.is_(True),
            )
        )
    ).one()
    return {"documents": int(row[0]), "chunks": int(row[1])}


__all__ = [
    "UPCOMING_WINDOW",
    "AttentionFailure",
    "AttentionFollowUp",
    "AttentionHandoff",
    "FollowUpCounts",
    "HandoffCounts",
    "LeadCounts",
    "RecentConversation",
    "active_handoffs",
    "follow_up_counts",
    "handoff_counts",
    "knowledge_counts",
    "lead_counts",
    "meta_connection_health",
    "pending_follow_ups",
    "recent_conversations",
    "recent_failures",
]
