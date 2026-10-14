"""The durable human handoff: a conversation escalated to a person.

Phase 5 *decided* to hand off - it set `Lead.status = HUMAN_HANDOFF` and recorded the
reason on the AI run - but nothing durable represented the request. The lead status is a
flag on a business entity; it cannot say who was asked, when, whether anyone acknowledged,
or whether the person eventually replied. A human agent needs all of that, and an operator
needs a queue. This table is that queue, and it is the single authority on whether a
conversation is currently in human hands.

The one-active-handoff invariant
--------------------------------
    CREATE UNIQUE INDEX ... ON human_handoffs (conversation_id)
        WHERE status IN ('OPEN','ASSIGNED','IN_PROGRESS')

A *partial* unique index, and the distinction from Phase 7 matters enough to spell out.
Phase 7's follow-up uniqueness is permanent: one conversation gets one follow-up per step,
ever. Applying that here would be wrong. A handoff is a lifecycle, not a slot - a lead can
be escalated, resolved, and genuinely escalated again next month, and the second escalation
is a new piece of work for a human, not a duplicate of the first. Permanent uniqueness
would silently swallow it. So the constraint is scoped to *active* statuses, and RESOLVED /
CANCELLED rows accumulate as history.

Making it a database constraint rather than a `SELECT` followed by an `INSERT` is the point.
The obvious sequence - look for an active handoff, and if there is none create one - has a
read-then-write window, and handoff creation is triggered from webhook processing, which
Meta retries, and from AI runs, which are retried. Ten workers processing one event would
all read "none" and all insert. The partial index means nine of them lose the insert and
read the winner back, so a repeated webhook produces one handoff, one notification and one
acknowledgement no matter how many times it is delivered.

Idempotency of the whole sequence, not just the row
---------------------------------------------------
The unique index only makes the *record* unique. The notification is separately keyed
`UNIQUE (handoff_id, type)`, and the acknowledgement is only ever sent on the transition
from no-handoff to handoff - so a duplicate event re-reads the existing row, sends
nothing, and notifies nobody. All three fall out of "only the creator acts".

The lead is a *reference*, not a copy
-------------------------------------
`lead_id` and `conversation_id` are composite `(tenant_id, id)` foreign keys, the pattern
Phases 2-7 established, so a correctly-tenant-scoped insert still cannot attach this
handoff to another tenant's conversation. The context columns on the row
(`lead_name`, `latest_inbound_text`, `qualification_total`) are a deliberately small
snapshot: a handoff that is only fully informative if you also fetch the conversation is not
a queue item an agent can work from. They are the fields a human needs to decide whether to
take it, and nothing more - no message history, no form answers, no embeddings.

Terminal states
---------------
`RESOLVED` and `CANCELLED` are terminal. There is no reopen. A later escalation creates a
*new* handoff row (the partial index permits exactly that), which is both the auditable
history a CRM-shaped product wants and the only way to avoid the "who un-resolved this and
when" question that has no answer in this data model.
"""

import enum
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class HandoffStatus(enum.StrEnum):
    """The lifecycle. `RESOLVED` and `CANCELLED` are terminal.

    `CANCELLED` is vocabulary for a terminal state this phase does not currently produce:
    there is no product event that says "the escalation was withdrawn". It is here because
    a status machine that cannot express "stopped for a reason other than success" forces
    the next phase to overload `RESOLVED` with a `resolution_note` that means the opposite
    thing. No Phase 8 code path writes it, deliberately - inventing a producer would be a
    feature nobody asked for.
    """

    OPEN = "OPEN"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    RESOLVED = "RESOLVED"
    CANCELLED = "CANCELLED"


#: Statuses during which a human owns the conversation and the AI must stand down.
#: A single constant, because "is a handoff active?" is asked from three different places
#: (creation, follow-up eligibility, AI suppression) and three copies of the literal list
#: would eventually disagree.
ACTIVE_HANDOFF_STATUSES: tuple[HandoffStatus, ...] = (
    HandoffStatus.OPEN,
    HandoffStatus.ASSIGNED,
    HandoffStatus.IN_PROGRESS,
)

#: The SQL predicate mirroring `ACTIVE_HANDOFF_STATUSES`. Used to build the partial unique
#: index, and it must stay in step with it.
ACTIVE_HANDOFF_PREDICATE = "status IN ('OPEN', 'ASSIGNED', 'IN_PROGRESS')"


class HandoffReason(enum.StrEnum):
    """Why this was escalated. An enum, so the value is never an unvalidated string.

    Phase 5 already distinguishes two of these cases in the shape of its own logic - a
    model that set `human_handoff` itself, and a total that crossed
    `ai_handoff_score_threshold` - and names the second in its `handoff_reason` text. Both
    collapse to `HIGH_INTENT` here, because the AI's free-text reason is kept in `detail`
    rather than promoted into the machine-readable vocabulary.
    """

    #: The model set `human_handoff` because the lead asked for a person.
    EXPLICIT_HUMAN_REQUEST = "EXPLICIT_HUMAN_REQUEST"
    #: Qualification crossed the configured threshold, with or without an explicit request.
    HIGH_INTENT = "HIGH_INTENT"
    #: The AI pipeline could not complete: provider error, or an unparseable answer.
    AI_FAILURE = "AI_FAILURE"
    #: Anything else a caller asserts. Deliberately rare, and always paired with `detail`.
    OTHER = "OTHER"


#: Higher wins. Why these numbers: an explicit "let me talk to someone" is a lead who has
#: already decided they want to buy and wants the friction removed, so it outranks a score
#: that merely crossed a line. A provider outage is last because the conversation is
#: usually still answerable by a person later, whereas a waiting lead costs the deal now.
REASON_PRIORITY: dict[HandoffReason, int] = {
    HandoffReason.EXPLICIT_HUMAN_REQUEST: 300,
    HandoffReason.HIGH_INTENT: 200,
    HandoffReason.OTHER: 100,
    HandoffReason.AI_FAILURE: 50,
}


class HumanHandoff(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "human_handoffs"

    tenant_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    # Nullable, MATCH SIMPLE: someone can message a page without ever submitting an ad
    # form, and an escalation from that person is perfectly ordinary.
    lead_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))

    status: Mapped[HandoffStatus] = mapped_column(
        SAEnum(HandoffStatus, name="handoff_status", native_enum=True),
        nullable=False,
        default=HandoffStatus.OPEN,
    )
    reason: Mapped[HandoffReason] = mapped_column(
        SAEnum(HandoffReason, name="handoff_reason", native_enum=True),
        nullable=False,
    )
    #: The originating explanation, free text and bounded. Phase 5's own
    #: `handoff_reason` string, or a failure category. Diagnostic and audit only - never
    #: the queue ordering, which is `priority`.
    detail: Mapped[str | None] = mapped_column(String(512))
    #: Higher sorts first. Derived from `reason` at creation; not operator-editable, because
    #: a hand-tuned priority on an escalation queue is a way to hide a lead.
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)

    #: Who owns it. NULL while OPEN. Set by a claim (self-assignment) or an assign
    #: (someone else put it on an agent).
    assigned_user_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    assigned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: When the lead was told a person would be in touch. Set on a successful send, and
    #: deliberately NOT set when the send failed - "we told them" and "we tried to tell
    #: them" are different facts and an operator must be able to tell them apart.
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Why the acknowledgement was not delivered, if it was not. A category, never a Meta
    #: payload, matching `messages.failure_reason`.
    acknowledgement_failure: Mapped[str | None] = mapped_column(String(64))
    #: `AI_REPLY` when the AI's own delivered message was accepted as the acknowledgement,
    #: `TEMPLATE` when the deterministic message was sent. Stored so a test - and an
    #: operator - can tell "the lead was told" from "we sent a fallback".
    acknowledgement_source: Mapped[str | None] = mapped_column(String(32))

    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution_note: Mapped[str | None] = mapped_column(Text)

    # -- small context snapshot, so the row is actionable on its own -------------
    lead_name: Mapped[str | None] = mapped_column(String(255))
    #: The most recent inbound text, truncated. Bounded on purpose: this is a queue
    #: preview, not a transcript.
    latest_inbound_text: Mapped[str | None] = mapped_column(Text)
    latest_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: The AI's total at the moment of escalation, when there was one.
    qualification_total: Mapped[int | None] = mapped_column(Integer)
    #: Traceability back to the event that caused this. Both nullable: a handoff can be
    #: created by a path that has neither.
    source_message_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    ai_run_id: Mapped[uuid.UUID | None] = mapped_column(PGUUID(as_uuid=True))
    #: Everything the state machine needs, in one document, so a transition is validated
    #: atomically without a second query. See `handoffs.py` for why this is not redundant.
    context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    __table_args__ = (
        # The whole point. Partial, so a resolved escalation does not block the next one.
        Index(
            "uq_human_handoffs_active_conversation",
            "conversation_id",
            unique=True,
            postgresql_where=text(ACTIVE_HANDOFF_PREDICATE),
        ),
        ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_human_handoffs_conversation_tenant",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "lead_id"],
            ["leads.tenant_id", "leads.id"],
            name="fk_human_handoffs_lead_tenant",
        ),
        # NOT a composite tenant reference, and the reason is worth stating because it looks
        # like an omission. `users` is a *global* identity table - a person belongs to
        # several tenants through `tenant_memberships`, so `users` has no `tenant_id` to
        # reference and cannot be given one without modelling users as tenant-owned, which
        # is the opposite of what they are.
        #
        # So the assignee is a plain FK, and cross-tenant assignment is prevented by two
        # independent things instead of one constraint: RLS already means a caller cannot
        # read or write a handoff outside their tenant, and the service separately verifies
        # that the target user holds an active membership in *this* tenant. The second is
        # not redundant with the first - it is the only thing that can catch a globally
        # valid user id that simply belongs to a different tenant.
        ForeignKeyConstraint(
            ["assigned_user_id"],
            ["users.id"],
            name="fk_human_handoffs_assigned_user",
            ondelete="SET NULL",
        ),
        # The queue query: one tenant's active work, highest priority first.
        Index("ix_human_handoffs_tenant_status", "tenant_id", "status"),
        # "What is on my plate", which is the second thing an agent asks.
        Index("ix_human_handoffs_tenant_assignee", "tenant_id", "assigned_user_id"),
        # Newest-first listing and default ordering.
        Index("ix_human_handoffs_tenant_created", "tenant_id", "created_at"),
    )

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_HANDOFF_STATUSES


__all__ = [
    "ACTIVE_HANDOFF_PREDICATE",
    "ACTIVE_HANDOFF_STATUSES",
    "REASON_PRIORITY",
    "HandoffReason",
    "HandoffStatus",
    "HumanHandoff",
]
