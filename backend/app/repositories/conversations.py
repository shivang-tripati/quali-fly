"""Tenant-scoped conversation and message access.

`find_or_create` and `insert_if_new` are the two race-safe boundaries, and they mirror
the ones Phase 3 established for leads: the database decides, never a check followed by an
insert. Two concurrent deliveries of the same `mid`, or two workers opening the same
thread, would both get past a read-then-write.

Every other function is scoped by RLS alone. None of them filters on `tenant_id`.
"""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.conversation import (
    Channel,
    Conversation,
    Message,
    MessageDelivery,
    MessageDirection,
)
from app.models.lead import Lead


async def find_or_create(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    meta_connection_id: uuid.UUID,
    channel: Channel,
    participant_id: str,
    source_id: str | None,
    referral_ad_id: str | None,
    now: datetime,
) -> tuple[Conversation, bool]:
    """Get the thread for this person, creating it on first contact.

    `ON CONFLICT DO NOTHING` against `uq_conversations_thread` is what makes this safe
    under concurrency: the first writer wins, the second re-reads the winner's row. A
    `SELECT`-then-`INSERT` would produce two threads for one person.
    """
    statement = (
        insert(Conversation)
        .values(
            tenant_id=tenant_id,
            meta_connection_id=meta_connection_id,
            channel=channel,
            meta_participant_id=participant_id,
            meta_source_id=source_id,
            # Only the first message's referral is kept; later ones are not evidence of
            # anything the conversation does not already record.
            meta_referral_ad_id=referral_ad_id,
            last_message_at=now,
            last_inbound_at=now,
        )
        .on_conflict_do_nothing(
            index_elements=["meta_connection_id", "channel", "meta_participant_id"]
        )
        .returning(Conversation.id)
    )
    inserted_id = (await session.execute(statement)).scalar_one_or_none()
    if inserted_id is not None:
        return await get(session, inserted_id), True

    existing = await session.execute(
        select(Conversation).where(
            Conversation.meta_connection_id == meta_connection_id,
            Conversation.channel == channel,
            Conversation.meta_participant_id == participant_id,
        )
    )
    return existing.scalar_one(), False


async def get(session: AsyncSession, conversation_id: uuid.UUID) -> Conversation | None:
    result = await session.execute(select(Conversation).where(Conversation.id == conversation_id))
    return result.scalar_one_or_none()


async def get_by_thread(
    session: AsyncSession,
    *,
    meta_connection_id: uuid.UUID,
    channel: Channel,
    participant_id: str,
) -> Conversation | None:
    result = await session.execute(
        select(Conversation).where(
            Conversation.meta_connection_id == meta_connection_id,
            Conversation.channel == channel,
            Conversation.meta_participant_id == participant_id,
        )
    )
    return result.scalar_one_or_none()


async def touch(
    session: AsyncSession,
    conversation: Conversation,
    *,
    now: datetime,
    inbound: bool,
) -> Conversation:
    """Move the thread's recency and window markers forward.

    `last_inbound_at` is only advanced by inbound messages, because it is what the
    24-hour window is measured from - an outbound message must not extend the window we
    are allowed to answer in.
    """
    values: dict[str, Any] = {"last_message_at": now}
    if inbound:
        values["last_inbound_at"] = now
    await session.execute(
        update(Conversation).where(Conversation.id == conversation.id).values(**values)
    )
    for field, value in values.items():
        setattr(conversation, field, value)
    await session.flush()
    return conversation


async def list_conversations(
    session: AsyncSession,
    *,
    limit: int = 50,
    lead_id: uuid.UUID | None = None,
) -> list[tuple[Conversation, str | None]]:
    """Most recently active first, each paired with its lead's name.

    Returns `(conversation, lead_name)` rather than bare rows because the inbox is the one
    screen that shows a list of people, and a list labelled with `meta_participant_id` makes
    the operator match `IG-48392011` against a name they can actually remember. The name is
    joined here so the client never has to fetch every lead to render a thread list, and it
    is an outer join because a thread can exist before a lead is attached to it.

    `lead_id` exists so the lead workspace can open the right thread in one request. Without
    it a lead detail page has to fetch the whole inbox and find the matching row, which is a
    second round trip and a filter in the browser.
    """
    statement = (
        select(Conversation, Lead.name)
        .outerjoin(Lead, Lead.id == Conversation.lead_id)
        .order_by(Conversation.last_message_at.desc().nulls_last(), Conversation.created_at.desc())
    )
    if lead_id is not None:
        statement = statement.where(Conversation.lead_id == lead_id)
    result = await session.execute(statement.limit(limit))
    return [(conversation, name) for conversation, name in result.all()]


async def get_with_lead_name(
    session: AsyncSession, conversation_id: uuid.UUID
) -> tuple[Conversation, str | None] | None:
    """One thread plus its lead's name, for the deep link.

    `get` cannot do this: it returns a bare `Conversation` and is used by the send path,
    which has no interest in a lead name. Rather than widen that one, the read that needs
    the name asks for the name.
    """
    result = await session.execute(
        select(Conversation, Lead.name)
        .outerjoin(Lead, Lead.id == Conversation.lead_id)
        .where(Conversation.id == conversation_id)
    )
    return result.one_or_none()


async def count_conversations(session: AsyncSession) -> int:
    result = await session.execute(select(func.count()).select_from(Conversation))
    return result.scalar_one()


# --- messages ----------------------------------------------------------------


async def insert_inbound_if_new(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    channel: Channel,
    meta_message_id: str,
    message_type: str,
    text: str | None,
    attachments: list[dict[str, Any]],
    sent_at: datetime,
) -> tuple[Message, bool]:
    """Store an inbound message unless this thread already has that `mid`.

    Returns `(message, created)`. `created=False` is the expected outcome for a Meta
    delivery retry, not an error.
    """
    statement = (
        insert(Message)
        .values(
            tenant_id=tenant_id,
            conversation_id=conversation_id,
            channel=channel,
            direction=MessageDirection.INBOUND,
            delivery=MessageDelivery.SENT,
            meta_message_id=meta_message_id,
            message_type=message_type,
            text=text,
            attachments=attachments,
            sent_at=sent_at,
        )
        .on_conflict_do_nothing(index_elements=["conversation_id", "direction", "meta_message_id"])
        .returning(Message.id)
    )
    inserted_id = (await session.execute(statement)).scalar_one_or_none()
    if inserted_id is not None:
        return await get_message(session, inserted_id), True

    existing = await session.execute(
        select(Message).where(
            Message.conversation_id == conversation_id,
            Message.direction == MessageDirection.INBOUND,
            Message.meta_message_id == meta_message_id,
        )
    )
    return existing.scalar_one(), False


async def create_outbound_pending(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    conversation_id: uuid.UUID,
    channel: Channel,
    text: str,
    sent_at: datetime,
    scheduled_task_id: uuid.UUID | None = None,
) -> Message:
    """Record the intent to send *before* the call goes out.

    If the process dies between here and the send, the PENDING row is what makes the
    attempt visible instead of silently lost. It carries no `meta_message_id` yet, which is
    why several PENDING rows can coexist under the dedupe index.

    `scheduled_task_id` attributes the attempt to the task that caused it, which is what
    lets a worker that inherited a crashed task know the message already went out. The
    partial unique index on `messages` makes a second successful outbound for the same task
    impossible to record, so this is the enforcement point rather than the intent.
    """
    message = Message(
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        channel=channel,
        direction=MessageDirection.OUTBOUND,
        delivery=MessageDelivery.PENDING,
        message_type="text",
        text=text,
        attachments=[],
        sent_at=sent_at,
        scheduled_task_id=scheduled_task_id,
    )
    session.add(message)
    await session.flush()
    return message


async def task_already_sent(
    session: AsyncSession, *, tenant_id: uuid.UUID, scheduled_task_id: uuid.UUID
) -> Message | None:
    """The *delivered* outbound this task already produced, if there is one.

    Only `SENT` counts. The other two delivery states mean the opposite of delivered:
    `PENDING` is an attempt that was recorded before the call and whose outcome was never
    seen - a worker that died there must be free to try again - and `FAILED` is a known
    non-delivery. Treating either as delivery would strand a task that has not actually
    messaged anybody.

    This is the local half of the exactly-once story and it is the whole of it. Meta's Send
    API offers no idempotency key, so nothing can make a *remote* send exactly-once; what can
    be guaranteed is that a task whose message was already accepted is never sent twice from
    this database, even if the worker that sent it died before recording completion.
    """
    return await session.scalar(
        select(Message)
        .where(
            Message.tenant_id == tenant_id,
            Message.scheduled_task_id == scheduled_task_id,
            Message.direction == MessageDirection.OUTBOUND,
            Message.delivery == MessageDelivery.SENT,
        )
        .order_by(Message.sent_at.desc())
        .limit(1)
    )


def apply_ai_attribution(session: AsyncSession, message: Message, attribution) -> None:
    """Attach real provider usage to a message row (Phase 5).

    Only `ai_model` is stored, plus the two counts and the latency. Provider *name* is
    deliberately not on `messages`; it lives on `ai_message_runs`, which is the record of
    a run. This keeps the hot inbox query narrow and leaves the audit trail in one place.

    No flush: the caller is mid-transaction and a flush here would be an extra round trip
    for no benefit. The subsequent `mark_message_sent`/`mark_message_failed` flush carries
    these columns with it.
    """
    message.ai_model = attribution.model
    message.ai_prompt_tokens = attribution.prompt_tokens
    message.ai_completion_tokens = attribution.completion_tokens
    message.ai_latency_ms = attribution.latency_ms


async def get_message(session: AsyncSession, message_id: uuid.UUID) -> Message | None:
    result = await session.execute(select(Message).where(Message.id == message_id))
    return result.scalar_one_or_none()


async def mark_message_sent(
    session: AsyncSession, message: Message, *, meta_message_id: str, at: datetime
) -> Message:
    message.delivery = MessageDelivery.SENT
    message.meta_message_id = meta_message_id
    message.delivered_at = at
    message.failure_reason = None
    await session.flush()
    return message


async def mark_message_failed(session: AsyncSession, message: Message, *, reason: str) -> Message:
    """A short category, never Meta's payload - see `Message.failure_reason`."""
    message.delivery = MessageDelivery.FAILED
    message.failure_reason = reason[:64]
    await session.flush()
    return message


async def get_message_by_provider_id(session: AsyncSession, meta_message_id: str) -> Message | None:
    """The outbound message a provider status refers to.

    Matched on `meta_message_id` because that is the only id the provider echoes back: a
    WhatsApp `statuses[]` entry names the `wamid` we stored at send time, and nothing else
    in the payload identifies our row. RLS scopes the read to this tenant, so a status for
    another tenant's message is simply not found.
    """
    result = await session.execute(
        select(Message).where(
            Message.meta_message_id == meta_message_id,
            Message.direction == MessageDirection.OUTBOUND,
        )
    )
    return result.scalars().first()


async def record_provider_delivery(
    session: AsyncSession, message: Message, *, at: datetime
) -> Message:
    """Meta confirmed delivery (or a read). Records the time, never re-sends.

    A status can arrive after a send already failed, and for a message that is already SENT
    this is purely additive. It deliberately does not flip a FAILED row back to SENT: the
    attempt failed, the attempt stays failed, and the delivery timestamp says what
    eventually happened. Rewriting history there would make a retry look like it had
    succeeded.
    """
    message.delivered_at = at
    await session.flush()
    return message


async def record_provider_failure(
    session: AsyncSession, message: Message, *, reason: str
) -> Message:
    """The provider reported this send as failed. A short category, never its payload."""
    if message.delivery is MessageDelivery.FAILED:
        # Already failed locally; a second provider failure adds nothing and would
        # overwrite a more specific local reason.
        return message
    return await mark_message_failed(session, message, reason=reason)


async def list_messages(
    session: AsyncSession, *, conversation_id: uuid.UUID, limit: int = 200
) -> list[Message]:
    """Oldest first, which is how a thread reads."""
    result = await session.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.sent_at, Message.id)
        .limit(limit)
    )
    return list(result.scalars().all())


async def count_messages(session: AsyncSession, conversation_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation_id)
    )
    return result.scalar_one()


__all__ = [
    "Channel",
    "count_conversations",
    "count_messages",
    "create_outbound_pending",
    "find_or_create",
    "get",
    "get_by_thread",
    "get_message",
    "get_with_lead_name",
    "insert_inbound_if_new",
    "list_conversations",
    "list_messages",
    "mark_message_failed",
    "mark_message_sent",
    "task_already_sent",
    "touch",
]
