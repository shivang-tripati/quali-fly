"""Durable scheduled work. PostgreSQL is the source of truth for this table.

APScheduler polls it. It is not the state: if the container is replaced, APScheduler's
in-memory job list is gone and nothing is lost, because the rows are here.

    conversation → PENDING task row → poller claims → PENDING→PROCESSING
                                                 ↓ (no lock held)
                                        AI + Meta send
                                                 ↓
                              COMPLETED / FAILED / CANCELLED

Three decisions in the schema are load-bearing, and each one closes a specific failure:

**`UNIQUE (conversation_id, task_type, sequence_step)`.** A conversation can have at most
one follow-up per step, ever. That single constraint is what makes scheduling idempotent
under concurrency: two schedulers racing to create Follow-Up #1 produce one row and one
loser, with no read-then-write window for the race to land in. It also means a
re-scheduling after a *completed* step is a no-op rather than a second message to a lead.

**`scheduled_at` and `status` are ordinary columns with an index on them.** The poller's
query is "PENDING rows due at or before now", and it is the only hot query. A partial index
on `(scheduled_at)` restricted to PENDING keeps that scan proportional to the number of
tasks that actually need attention, not to the table's history.

**`locked_at` is a lease, not a mutex.** A claimed task is `PROCESSING` with a lease
expiry. If a worker dies mid-execute, the row does not hang forever: once `locked_at` is
older than the lease, another worker may reclaim it. `locked_by` records which worker
holds it, purely for diagnosis.

The tenant relationship is a composite `(conversation_id, tenant_id)` foreign key, the
pattern Phases 2-6 established, so a correctly-tenant-scoped insert still cannot attach a
task to another tenant's conversation. `lead_id` is nullable because a conversation may
have no lead, and is deliberately not required.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class TaskType(enum.StrEnum):
    """What a task is for.

    `FOLLOW_UP` is the PRD's. `GHOST` exists because the 7-day transition is part of the
    same sequence and needs the same durability and the same cancellation: a lead who
    replies on day 6 must not be marked ghosted on day 7, and only a cancellable row can
    guarantee that.
    """

    FOLLOW_UP = "FOLLOW_UP"
    GHOST = "GHOST"


class TaskStatus(enum.StrEnum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ScheduledTask(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "scheduled_tasks"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Nullable: a conversation may have no lead, and a follow-up for a person who never
    # submitted an ad form is still a legitimate thing to send.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    task_type: Mapped[TaskType] = mapped_column(SAEnum(TaskType, name="task_type"), nullable=False)
    # 1, 2, 3 for FOLLOW_UP; 1 for GHOST. Combined with the unique constraint above, this
    # is the whole idempotency key.
    sequence_step: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    status: Mapped[TaskStatus] = mapped_column(
        SAEnum(TaskStatus, name="task_status"),
        nullable=False,
        default=TaskStatus.PENDING,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)

    # Lease fields. `locked_at` is the expiry basis, not a mutex.
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))

    # A short category, never a provider or Meta payload. Scheduler logs are read by
    # operators, and a provider error can echo prompt or message content.
    last_error: Mapped[str | None] = mapped_column(String(64))
    # Rendered body actually sent, so an operator can see what a lead received. Bounded,
    # and it holds our own outbound text rather than anything Meta returned.
    sent_text: Mapped[str | None] = mapped_column(Text)
    #: Whether the body came from the AI or the deterministic template. Diagnostic: it
    #: answers "did the AI enhancement actually do anything?".
    ai_enhanced: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The inbound timestamp this task's sequence is anchored to. The eligibility re-check
    #: compares the conversation's `last_inbound_at` against this, which is how a reply
    #: that lands *after* the task was created invalidates it even when cancellation
    #: already happened.
    activity_anchor_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Referenced by `messages.fk_messages_scheduled_task_tenant`, so a message cannot
        # point at a task belonging to a different tenant. Same shape as
        # `messages.uq_messages_id_tenant`.
        UniqueConstraint("id", "tenant_id", name="uq_scheduled_tasks_id_tenant"),
        UniqueConstraint(
            "conversation_id",
            "task_type",
            "sequence_step",
            name="uq_scheduled_tasks_conversation_type_step",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_scheduled_tasks_conversation_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "lead_id"],
            ["leads.tenant_id", "leads.id"],
            name="fk_scheduled_tasks_lead_tenant",
        ),
        # The poller's only hot query: due PENDING rows for one tenant. Partial, because
        # completed and cancelled tasks outnumber pending ones by orders of magnitude once
        # a tenant has been running for a while.
        Index(
            "ix_scheduled_tasks_due",
            "tenant_id",
            "scheduled_at",
            postgresql_where=text("status = 'PENDING'"),
        ),
        # Lease recovery: PROCESSING rows whose lease has expired. A worker that dies
        # mid-execute must not leave a task stuck forever.
        Index(
            "ix_scheduled_tasks_lease",
            "locked_at",
            postgresql_where=text("status = 'PROCESSING'"),
        ),
    )


__all__ = ["ScheduledTask", "TaskStatus", "TaskType"]
