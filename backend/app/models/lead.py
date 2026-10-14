"""The Lead: one Meta Lead Ads submission, owned by one tenant.

This is the first *business* entity in the product and the anchor for everything that
follows (conversations, qualification, follow-ups, handoff), so it is deliberately
narrow: a normalized identity, the provider metadata a tenant will actually filter
on, and the provider's own form answers. It is not a universal CRM contact.

Retention note: `field_data` holds the answers a person typed into a Meta lead form -
name, email, phone, and whatever else the tenant's form asked for. That is personal
data, so it lives behind the same tenant RLS as everything else and is never returned
by any Phase 3 endpoint except the tenant's own lead list. The raw Graph response is
*not* duplicated here: it is already stored once, on the webhook event, for audit.

`status` and `qualification` are PRD fields (Sprint 3.1). Phase 3 only ever writes
`NEW` and leaves `qualification` null; the transition engine and the AI that fills it
in belong to later phases, so the vocabulary exists without the rules.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class LeadSource(enum.StrEnum):
    META_LEAD_ADS = "META_LEAD_ADS"


class LeadStatus(enum.StrEnum):
    """PRD Sprint 3.4 lifecycle. Phase 3 creates leads as NEW and nothing else."""

    NEW = "NEW"
    REPLIED = "REPLIED"
    QUALIFIED = "QUALIFIED"
    CLOSED = "CLOSED"
    GHOSTED = "GHOSTED"
    HUMAN_HANDOFF = "HUMAN_HANDOFF"


class Lead(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "leads"
    __table_args__ = (
        # The PRD's deduplication rule, and the final authority on it. Meta retries
        # webhooks and we replay events by hand, so a read-then-write would race; the
        # database has to be the one that says no.
        Index("uq_leads_tenant_meta_leadgen_id", "tenant_id", "meta_leadgen_id", unique=True),
        Index("ix_leads_tenant_id", "tenant_id"),
        Index("ix_leads_tenant_status", "tenant_id", "status"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Which Meta connection produced this lead. `SET NULL`, not CASCADE: disconnecting
    # a connection must not delete the tenant's leads.
    meta_connection_id: Mapped[uuid.UUID | None] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("meta_connections.id", ondelete="SET NULL"),
    )

    # Meta's own id for this submission. Never null: it is the deduplication key.
    meta_leadgen_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[LeadSource] = mapped_column(
        SAEnum(LeadSource, name="lead_source", native_enum=True),
        nullable=False,
        default=LeadSource.META_LEAD_ADS,
    )
    status: Mapped[LeadStatus] = mapped_column(
        SAEnum(LeadStatus, name="lead_status", native_enum=True),
        nullable=False,
        default=LeadStatus.NEW,
    )

    # -- contact fields, normalized out of Meta's form answers ---------------
    name: Mapped[str | None] = mapped_column(String(255))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(64))

    # -- provider metadata worth filtering on -------------------------------
    form_id: Mapped[str | None] = mapped_column(String(64))
    ad_id: Mapped[str | None] = mapped_column(String(64))
    # Meta's own creation timestamp, which is the lead's real time and is not
    # necessarily the time we received the webhook.
    provider_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # The form's answers, as {field_name: first_value}. Personal data; see the module
    # docstring.
    field_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # PRD Sprint 3.1 field. Written by the AI qualification phase, not by Phase 3.
    qualification: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    # Why processing stopped, when it did. Safe to surface: categories only.
    ingestion_note: Mapped[str | None] = mapped_column(Text)
