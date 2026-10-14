"""Conversations and messages: the second core pillar.

`Conversation` is one thread between a tenant and one person on one Meta surface.
`Message` is one turn in it, inbound or outbound.

Two things about Meta's messaging model shape this schema, and both were verified against
Meta's developer documentation on 2026-09-28:

**A thread is keyed by the participant, not by a thread id.** The messaging webhook
carries `sender.id` (a Page-scoped id, or an Instagram-scoped id) and no `thread` object
at all. For a given page, one participant id *is* one thread, which is why the unique key
is (connection, channel, participant) and why `meta_participant_id` is mandatory: it is
also the recipient id every outbound send needs.

**Messenger and Instagram share the send endpoint.** Both send via
`POST /{page_id}/messages`; only the recipient id and the required permission differ. So
`Channel` is a data distinction, not an architectural one - see `app/services/channels.py`.

The 24-hour messaging window is a real product constraint, not an error to discover
later: Meta refuses a `RESPONSE` send after 24 hours with error 1545041. `last_inbound_at`
is therefore a first-class column, because it is what the window is measured from, and
Phase 7's follow-up engine will need it for the same reason.

`lead_id` is deliberately nullable and is *not* auto-populated. Meta's only documented
link from a messaging event back to an ad is `referral.ad_id` (kept in
`meta_referral_ad_id`), and a single ad produces many leads, so matching on it would
attach the wrong person to the wrong lead. Storing the evidence is useful; guessing the
conclusion is not. See the module's use in `conversations.py`.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Index, String, Text, text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class Channel(enum.StrEnum):
    """A Meta surface. Not an engine - see the module docstring."""

    MESSENGER = "MESSENGER"
    INSTAGRAM = "INSTAGRAM"
    # Phase 10-O.3. WhatsApp is the first surface that does NOT share the page messaging
    # endpoint: a send goes to `/{phone_number_id}/messages` and the participant id is a
    # WhatsApp id, not a Page-scoped one. So it is a distinct value here, and
    # `app/services/channels.py` is where that difference is expressed - not a second
    # conversation engine.
    WHATSAPP = "WHATSAPP"


class MessageDirection(enum.StrEnum):
    INBOUND = "INBOUND"
    OUTBOUND = "OUTBOUND"


class MessageDelivery(enum.StrEnum):
    """Delivery state, which only outbound messages really have.

    PENDING exists because a send is persisted *before* the outbound call: if the process
    dies between the two, the record of intent survives. Marking it SENT afterwards with
    Meta's `message_id` is what makes a retry safe - and a PENDING row that is never
    resolved is visible rather than silently lost.
    """

    PENDING = "PENDING"
    SENT = "SENT"
    FAILED = "FAILED"


class Conversation(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "conversations"
    __table_args__ = (
        # The thread identity. Meta gives no thread id, so participant + surface is the
        # only thing that identifies a conversation, and this index is also the
        # race-safe guard for find-or-create.
        Index(
            "uq_conversations_thread",
            "meta_connection_id",
            "channel",
            "meta_participant_id",
            unique=True,
        ),
        Index("ix_conversations_tenant_id", "tenant_id"),
        # Most-recent-first is the only ordering the product needs so far.
        Index("ix_conversations_tenant_last_message", "tenant_id", "last_message_at"),
        Index("ix_conversations_lead_id", "lead_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # SET NULL, like `leads`: disconnecting a Meta connection must not delete the
    # tenant's history of what people said to them.
    meta_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("meta_connections.id", ondelete="SET NULL"),
    )
    # Nullable on purpose - see the module docstring for why Phase 4 does not guess it.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("leads.id", ondelete="SET NULL"),
    )

    channel: Mapped[Channel] = mapped_column(
        SAEnum(Channel, name="channel", native_enum=True),
        nullable=False,
    )
    # Page-scoped id (Messenger) or Instagram-scoped id (Instagram) of the person on the
    # other end. Also the `recipient.id` of every outbound send.
    meta_participant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    # The external id the event was routed by: a page id, or an Instagram business id.
    # Not the send target - that is the connection's `page_id` for both channels.
    meta_source_id: Mapped[str | None] = mapped_column(String(64))
    # Meta's `referral.ad_id`, the only ad-level provenance a messaging event carries.
    # Stored as evidence; deliberately not resolved into `lead_id` automatically.
    meta_referral_ad_id: Mapped[str | None] = mapped_column(String(64))

    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # The 24-hour messaging window is measured from here.
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Message(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "messages"
    __table_args__ = (
        # Inbound deduplication. Meta retries webhook deliveries, and `messaging[]` is an
        # array, so the same mid can arrive more than once. The unique index is the
        # authority, because a read-then-write would race two deliveries.
        #
        # Postgres treats NULLs as distinct, so several PENDING outbound rows (which have
        # no Meta id yet) do not collide.
        Index(
            "uq_messages_dedupe",
            "conversation_id",
            "direction",
            "meta_message_id",
            unique=True,
        ),
        Index("ix_messages_tenant_id", "tenant_id"),
        # Tenant-carrying foreign key: a message cannot point at a task in another tenant,
        # which RLS does not cover for a reference column.
        ForeignKeyConstraint(
            ["tenant_id", "scheduled_task_id"],
            ["scheduled_tasks.tenant_id", "scheduled_tasks.id"],
            ondelete="CASCADE",
            name="fk_messages_scheduled_task_tenant",
        ),
        # At most one *delivered* outbound per scheduled task, ever. Only SENT is constrained:
        # PENDING means the attempt was recorded but its outcome is unknown (a worker may have
        # died before calling Meta, and must be free to retry), and FAILED is a known
        # non-delivery that a bounded retry legitimately records more than once. Constraining
        # either would block a legitimate retry at the database.
        Index(
            "uq_messages_task_outbound_sent",
            "scheduled_task_id",
            unique=True,
            postgresql_where=text(
                "scheduled_task_id IS NOT NULL AND direction = 'OUTBOUND' AND delivery = 'SENT'"
            ),
        ),
        Index(
            "ix_messages_scheduled_task_id",
            "scheduled_task_id",
            postgresql_where=text("scheduled_task_id IS NOT NULL"),
        ),
        # Oldest-first within a conversation, which is how a thread reads.
        Index("ix_messages_conversation_sent_at", "conversation_id", "sent_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # CASCADE: a message has no meaning without its conversation, and conversations are
    # never deleted - they are deactivated by disconnecting the connection.
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Denormalized from the conversation because the PRD lists platform on Message, and
    # because filtering a message stream by channel should not need a join. The engine is
    # the only writer, and a test asserts the two never disagree.
    channel: Mapped[Channel] = mapped_column(
        SAEnum(Channel, name="message_channel", native_enum=True),
        nullable=False,
    )
    direction: Mapped[MessageDirection] = mapped_column(
        SAEnum(MessageDirection, name="message_direction", native_enum=True),
        nullable=False,
    )
    delivery: Mapped[MessageDelivery] = mapped_column(
        SAEnum(MessageDelivery, name="message_delivery", native_enum=True),
        nullable=False,
        default=MessageDelivery.SENT,
    )

    # Meta's own id. Inbound: the webhook `mid`. Outbound: the `message_id` from the
    # send response.
    meta_message_id: Mapped[str | None] = mapped_column(String(255))
    # One of Meta's documented message/attachment types (text, image, audio, ...). A
    # String rather than an enum so a type Meta adds next quarter is not a migration.
    message_type: Mapped[str] = mapped_column(String(32), nullable=False, default="text")
    text: Mapped[str | None] = mapped_column(Text)

    # Attachment types and titles only. Meta's attachment URLs expire and can point at
    # personal media, so they are deliberately not retained; re-fetching is a later
    # concern and needs its own decision about retention.
    attachments: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False, default=list)

    # Provider timestamp. Inbound it is Meta's epoch-milliseconds value, not our
    # arrival time: those differ when a delivery is retried.
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # A category, never Meta's payload. 551 and 1545041 are worth acting on; a stack
    # trace from a provider is not.
    failure_reason: Mapped[str | None] = mapped_column(String(64))

    # -- AI attribution, written by the Phase 5 qualification engine -----------------
    # Declared here because the PRD lists them on Message, but nothing in Phase 4 writes
    # them: Phase 4 produces no AI output.
    ai_model: Mapped[str | None] = mapped_column(String(64))
    ai_prompt_tokens: Mapped[int | None] = mapped_column()
    ai_completion_tokens: Mapped[int | None] = mapped_column()
    ai_latency_ms: Mapped[int | None] = mapped_column()

    # -- Provenance, written by the Phase 7 follow-up engine -----------------------
    # Which scheduled task produced this message, if any. This is the join that lets a
    # worker which inherited a crashed task's work tell "already sent" from "never sent":
    # without it, a task reclaimed after a worker died between Meta accepting the message
    # and the task being completed has no local record that the lead was already messaged.
    # The tenant-carrying foreign key is declared in `__table_args__` because it spans two
    # columns; a partial unique index then makes one successful outbound per task a database
    # guarantee rather than a service-layer convention.
    scheduled_task_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
