"""Durable record of one AI attempt against one inbound message.

Why a table and not columns on `messages`: the work is asynchronous and can fail
independently of the message. A message row says "we received this"; it cannot also say
"an AI is currently answering this", "we tried twice and it is still failing", and
"which provider produced the reply" without a wide nullable column set that has to be
kept consistent by hand. A row per attempt, with a uniqueness constraint on the message,
makes idempotency a database property instead of a code convention.

`inbound_message_id` is UNIQUE, not merely indexed. That single constraint is what makes
the whole pipeline safe to retry: concurrent workers, a duplicated webhook, or a replayed
event all race to insert, exactly one wins, and the losers return the existing run
instead of calling a provider twice. Charging a lead two AI replies because two workers
picked up the same event is not a bug we can tolerate, so the database is the enforcement
point.

The column names are the PRD's terms, not ours: `high_intent` is stored separately from
`qualification_total` because the PRD defines high intent as *above a threshold*, and a
stored score plus a stored verdict lets us re-evaluate the rule later without re-running
the model.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Enum,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class AIRunStatus(enum.StrEnum):
    IN_PROGRESS = "IN_PROGRESS"
    PROCESSED = "PROCESSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class AIMessageRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One AI processing attempt for one inbound message."""

    __tablename__ = "ai_message_runs"

    # Redundant with conversation_id, but it makes tenant-scoped lookups by conversation
    # (the only query the pipeline actually performs) index-only.
    tenant_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    inbound_message_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # Nullable because a conversation may have no Lead. Deliberately not required.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    outbound_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    status: Mapped[AIRunStatus] = mapped_column(
        Enum(AIRunStatus, name="ai_run_status"),
        nullable=False,
        default=AIRunStatus.IN_PROGRESS,
    )
    # Coarse classification, not a message. Provider errors can echo prompt text and must
    # not land in a column an operator or an API will read back.
    error_category: Mapped[str | None] = mapped_column(String(64))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)

    qualification_total: Mapped[int | None] = mapped_column(Integer)
    high_intent: Mapped[bool] = mapped_column(nullable=False, default=False)
    human_handoff_required: Mapped[bool] = mapped_column(nullable=False, default=False)
    handoff_reason: Mapped[str | None] = mapped_column(String(500))
    # The validated, normalised result. Stored so a run is auditable and replayable
    # without re-billing a provider.
    ai_response: Mapped[dict | None] = mapped_column(JSONB)

    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # The idempotency guarantee. See the module docstring.
        UniqueConstraint("inbound_message_id", name="uq_ai_message_runs_inbound_message"),
        # Composite FKs carry the tenant id, so PostgreSQL itself refuses a row that
        # points at another tenant's conversation even if RLS were disabled. This is the
        # pattern the earlier phases established; matching it keeps the invariant
        # uniform instead of relying on each new table getting the RLS policy right.
        #
        # Names are suffixed `_id_tenant` to match what migration 0006 actually created -
        # autogenerate compares names literally, and a model that spelled these
        # differently would report permanent phantom drift on every run.
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_ai_message_runs_conversation_id_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "inbound_message_id"],
            ["messages.tenant_id", "messages.id"],
            name="fk_ai_message_runs_inbound_message_id_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "outbound_message_id"],
            ["messages.tenant_id", "messages.id"],
            name="fk_ai_message_runs_outbound_message_id_tenant",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "lead_id"],
            ["leads.tenant_id", "leads.id"],
            name="fk_ai_message_runs_lead_id_tenant",
        ),
        # Serves the "unprocessed inbound messages for this conversation" scan.
        Index(
            "ix_ai_message_runs_pending",
            "tenant_id",
            "conversation_id",
            postgresql_where="status = 'IN_PROGRESS'",
        ),
    )
