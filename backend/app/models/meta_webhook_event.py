"""Durable inbox of inbound Meta webhook events.

The webhook request authenticates, identifies, deduplicates and stores an event, then
ACKs. Nothing downstream happens here: a future processor claims rows and drives
`RECEIVED -> PROCESSING -> PROCESSED|FAILED`.

`payload` keeps the original event JSONB so Phase 3+ can read `leadgen_id` and build
domain objects without re-contacting Meta. No domain entity is derived from it here.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class MetaWebhookEventStatus(enum.StrEnum):
    RECEIVED = "RECEIVED"
    PROCESSING = "PROCESSING"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"


class MetaWebhookEvent(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "meta_webhook_events"
    __table_args__ = (
        # The final idempotency guard. The unique index on
        # (meta_connection_id, deduplication_key) is the real race-safe mechanism;
        # this constraint keeps the SQLAlchemy model honest.
        Index(
            "uq_meta_webhook_events_dedupe",
            "meta_connection_id",
            "deduplication_key",
            unique=True,
        ),
        Index("ix_meta_webhook_events_tenant_id", "tenant_id"),
        Index("ix_meta_webhook_events_status_received_at", "status", "received_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    meta_connection_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("meta_connections.id", ondelete="CASCADE"),
        nullable=False,
    )

    # Deterministic identity of the event, never a timestamp or a random value.
    deduplication_key: Mapped[str] = mapped_column(String(255), nullable=False)
    # Meta's own id when the payload carries a stable one (messaging mid).
    meta_event_id: Mapped[str | None] = mapped_column(String(255))

    object_type: Mapped[str | None] = mapped_column(String(64))
    event_type: Mapped[str | None] = mapped_column(String(128))
    # The external identifier the source was resolved from (page id, IG business id).
    source_identifier: Mapped[str | None] = mapped_column(String(64))

    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # Allowlisted, non-secret headers only. The signature is deliberately not stored.
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    status: Mapped[MetaWebhookEventStatus] = mapped_column(
        SAEnum(MetaWebhookEventStatus, name="meta_webhook_event_status", native_enum=True),
        nullable=False,
        default=MetaWebhookEventStatus.RECEIVED,
    )
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_message: Mapped[str | None] = mapped_column(Text)

    # Phase 10-D claim ownership. Without these, `claim_next_received` could set
    # `status = PROCESSING` but not say who did it, and a crash held that row forever.
    # `locked_by` is the claim token every terminal write is conditional on, and
    # `locked_at` is what makes the claim expire so another worker can recover it. Same
    # shape as `scheduled_tasks`, deliberately: two claim systems would be one too many.
    locked_by: Mapped[str | None] = mapped_column(String(128))
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # How many times this event has been claimed. Makes the retry budget a property of the
    # row rather than of whatever a caller happens to remember.
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
