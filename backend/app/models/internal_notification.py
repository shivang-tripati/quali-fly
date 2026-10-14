"""Internal notifications: what a human needs to know, inside the product.

Phase 8 needs an inbox. It does not need a notification *system* - there is no email, no
push, no Slack integration, and Phase 8 explicitly refuses to add any of them. So this is a
row a person reads in a list, and nothing more.

`UNIQUE (handoff_id, type)`
---------------------------
The idempotency key, and the reason this table cannot fan out into duplicates. Handoff
creation is driven by retried webhooks and retried AI runs, so "create a handoff" and
"tell someone" would otherwise run several times for one real event. Because notification
creation is keyed on the handoff that produced it, and only the thread that *created* the
handoff creates notifications, N duplicate events produce one handoff and one notification.

A row is never deleted, only read. A notification is evidence that a person was told, and
deleting it would make "was anyone notified?" unanswerable after the fact.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKeyConstraint, Index, String, UniqueConstraint
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class NotificationType(enum.StrEnum):
    """What happened. Narrow on purpose.

    Two values, because two things are worth interrupting someone about: a conversation
    needs a person, and we failed to tell the lead that. A third value for "a handoff was
    assigned" would be a notification about a notification, in a product with no
    preferences model to respect.
    """

    #: A conversation was escalated. The one notification the PRD requires.
    HANDOFF_CREATED = "HANDOFF_CREATED"
    #: The acknowledgement could not be delivered. Not an error the agent can fix by
    #: replying - it means the lead may not know anyone is coming, which changes how the
    #: agent should open the conversation.
    HANDOFF_ACK_FAILED = "HANDOFF_ACK_FAILED"


class NotificationStatus(enum.StrEnum):
    UNREAD = "UNREAD"
    READ = "READ"


class InternalNotification(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "internal_notifications"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    #: Always set. A notification is *about* a handoff; the standalone shape is a
    #: different feature and is not built on a guess about it.
    handoff_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    lead_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    type: Mapped[NotificationType] = mapped_column(
        SAEnum(NotificationType, name="notification_type", native_enum=True),
        nullable=False,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    #: Structured, render-ready context for the inbox row. Deliberately bounded and
    #: deliberately built only from fields the human needs to decide whether to take the
    #: work: who, why, what they said, how urgent.
    #:
    #: What is never here: access tokens, encrypted credentials, provider keys, embeddings,
    #: or the conversation transcript. This is a row in a tenant-scoped table that a
    #: dashboard will render, so the safe rule is that it holds nothing that would be a
    #: liability if it were displayed. The message *preview* is the single piece of
    #: customer text, truncated, because an agent cannot triage a lead they cannot see.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    status: Mapped[NotificationStatus] = mapped_column(
        SAEnum(NotificationStatus, name="notification_status", native_enum=True),
        nullable=False,
        default=NotificationStatus.UNREAD,
    )
    #: NULL means "the whole tenant". Set only when a notification is addressed to one
    #: person, which Phase 8 does not do - the inbox is shared. The column exists so
    #: addressing one later is a migration that adds a filter, not a redesign.
    recipient_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Idempotency. One HANDOFF_CREATED notification per handoff, ever, enforced by the
        # database rather than by whoever remembers to check.
        UniqueConstraint("handoff_id", "type", name="uq_internal_notifications_handoff_type"),
        ForeignKeyConstraint(
            ["tenant_id", "handoff_id"],
            ["human_handoffs.tenant_id", "human_handoffs.id"],
            name="fk_internal_notifications_handoff_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_internal_notifications_conversation_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "lead_id"],
            ["leads.tenant_id", "leads.id"],
            name="fk_internal_notifications_lead_tenant",
        ),
        Index("ix_internal_notifications_tenant_id", "tenant_id"),
        # The inbox query: unread first, newest first, for one tenant.
        Index("ix_internal_notifications_inbox", "tenant_id", "status", "created_at"),
    )


__all__ = ["InternalNotification", "NotificationStatus", "NotificationType"]
