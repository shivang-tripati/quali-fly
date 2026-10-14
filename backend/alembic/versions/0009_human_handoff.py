"""human handoff: human_handoffs and internal_notifications

Revision ID: 0009_human_handoff
Revises: 0008_follow_up_engine
Create Date: 2026-09-29

Turns Phase 5's `Lead.status = HUMAN_HANDOFF` flag into a durable, assignable queue.

Why a table and not a lead status
--------------------------------
Phase 5 already records the decision, but a lead status cannot say who was asked, when,
whether the lead was told, or whether anyone replied. It is also not queryable as work: an
agent needs a list of *open escalations*, which is a different thing from a list of leads.
`human_handoffs` is that list, and it is the single authority on whether a conversation is
currently in human hands - which is what makes the AI-suppression and follow-up-suppression
rules decidable without consulting a second table.

One active handoff per conversation
-----------------------------------
    CREATE UNIQUE INDEX uq_human_handoffs_active_conversation
        ON human_handoffs (conversation_id)
        WHERE status IN ('OPEN','ASSIGNED','IN_PROGRESS')

Partial, and the choice is deliberate. Phase 7's follow-up uniqueness is *permanent* - one
conversation gets one follow-up per step, ever - because re-sending a follow-up to someone
who already got one is a defect. A handoff is not that. A lead can be escalated, resolved,
and legitimately escalated again; the second escalation is new work for a human, and
permanent uniqueness would silently swallow it. So the constraint covers only the statuses
that mean "somebody still has to do something", and RESOLVED/CANCELLED accumulate as
history.

Being a database constraint rather than SELECT-then-INSERT is the load-bearing part. Handoff
creation is reached from webhook processing (which Meta retries) and from AI runs (which
are retried), so a read-then-write would let ten workers all observe "no active handoff" and
all insert. Here nine of them lose the insert and read the winner back, so a duplicated
event yields exactly one handoff, one notification and one acknowledgement.

Notification idempotency
------------------------
    UNIQUE (handoff_id, type)

Separate from the handoff constraint, and needed because they guard different things: the
partial index stops duplicate *handoffs*, this stops duplicate *notifications* for the one
that won. Since only the thread that created the handoff creates notifications, both are
idempotent for the same reason.

Tenant integrity
----------------
Composite `(tenant_id, id)` references throughout, the pattern Phases 2-8 established,
supported by the redundant-but-necessary `uq_human_handoffs_id_tenant` that Phase 5's
migration introduced for `messages` and `leads`.

`assigned_user_id` is deliberately a *plain* FK to `users.id` and not a composite one.
`users` is a global identity table with no `tenant_id` - a person belongs to several tenants
through `tenant_memberships` - so there is no tenant column to reference, and adding one
would model users as tenant-owned, which is the opposite of what they are. Cross-tenant
assignment is therefore prevented by two independent mechanisms rather than one constraint:
RLS means a caller cannot reach a handoff outside their tenant at all, and the service
separately verifies the target holds an active membership in *this* tenant. The membership
check is not redundant with RLS - it is the only thing that can reject a globally valid user
id that merely belongs to a different tenant.

Rollback
--------
`downgrade` drops the enum types explicitly. `drop_table` leaves a native enum behind and
the next `upgrade` fails with `type "handoff_status" already exists`.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009_human_handoff"
down_revision: str | None = "0008_follow_up_engine"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

TENANT_MATCH = "tenant_id = nullif(current_setting('app.current_tenant', true), '')::uuid"

ACTIVE_PREDICATE = "status IN ('OPEN', 'ASSIGNED', 'IN_PROGRESS')"

HANDOFF_STATUS = ("OPEN", "ASSIGNED", "IN_PROGRESS", "RESOLVED", "CANCELLED")
HANDOFF_REASON = ("EXPLICIT_HUMAN_REQUEST", "HIGH_INTENT", "AI_FAILURE", "OTHER")
NOTIFICATION_TYPE = ("HANDOFF_CREATED", "HANDOFF_ACK_FAILED")
NOTIFICATION_STATUS = ("UNREAD", "READ")


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"CREATE POLICY {table}_select ON {table} FOR SELECT USING ({TENANT_MATCH})")
    op.execute(f"CREATE POLICY {table}_insert ON {table} FOR INSERT WITH CHECK ({TENANT_MATCH})")
    op.execute(
        f"CREATE POLICY {table}_update ON {table} "
        f"FOR UPDATE USING ({TENANT_MATCH}) WITH CHECK ({TENANT_MATCH})"
    )
    op.execute(f"CREATE POLICY {table}_delete ON {table} FOR DELETE USING ({TENANT_MATCH})")


def _disable_rls(table: str) -> None:
    for suffix in ("delete", "update", "insert", "select"):
        op.execute(f"DROP POLICY IF EXISTS {table}_{suffix} ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    # -- human_handoffs ----------------------------------------------------
    op.create_table(
        "human_handoffs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "status",
            sa.Enum(*HANDOFF_STATUS, name="handoff_status"),
            nullable=False,
            server_default="OPEN",
        ),
        sa.Column("reason", sa.Enum(*HANDOFF_REASON, name="handoff_reason"), nullable=False),
        sa.Column("detail", sa.String(length=512)),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("assigned_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("assigned_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True)),
        sa.Column("acknowledgement_failure", sa.String(length=64)),
        sa.Column("acknowledgement_source", sa.String(length=32)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolution_note", sa.Text()),
        # Small, bounded context snapshot so a queue row is actionable on its own. No
        # transcript, no form answers, no credentials.
        sa.Column("lead_name", sa.String(length=255)),
        sa.Column("latest_inbound_text", sa.Text()),
        sa.Column("latest_inbound_at", sa.DateTime(timezone=True)),
        sa.Column("qualification_total", sa.Integer()),
        sa.Column("source_message_id", postgresql.UUID(as_uuid=True)),
        sa.Column("ai_run_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # The invariant. Partial, so a resolved escalation does not block the next one.
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_human_handoffs_conversation_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "lead_id"],
            ["leads.tenant_id", "leads.id"],
            name="fk_human_handoffs_lead_tenant",
        ),
        # Plain, not composite: `users` is global and has no tenant_id. See the module
        # docstring - cross-tenant assignment is prevented by RLS plus a membership check.
        sa.ForeignKeyConstraint(
            ["assigned_user_id"],
            ["users.id"],
            name="fk_human_handoffs_assigned_user",
            ondelete="SET NULL",
        ),
    )

    # Required for internal_notifications' composite reference, and the same
    # redundant-with-the-PK constraint Phase 5 introduced for messages and leads.
    op.create_unique_constraint(
        "uq_human_handoffs_id_tenant", "human_handoffs", ["id", "tenant_id"]
    )
    op.create_index(
        "uq_human_handoffs_active_conversation",
        "human_handoffs",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_PREDICATE),
    )
    # The queue query: one tenant's handoffs by status.
    op.create_index("ix_human_handoffs_tenant_status", "human_handoffs", ["tenant_id", "status"])
    # "What is on my plate".
    op.create_index(
        "ix_human_handoffs_tenant_assignee", "human_handoffs", ["tenant_id", "assigned_user_id"]
    )
    # Newest-first listing and default ordering.
    op.create_index(
        "ix_human_handoffs_tenant_created", "human_handoffs", ["tenant_id", "created_at"]
    )
    _enable_rls("human_handoffs")

    # -- internal_notifications -------------------------------------------
    op.create_table(
        "internal_notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("handoff_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "type",
            sa.Enum(*NOTIFICATION_TYPE, name="notification_type"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "status",
            sa.Enum(*NOTIFICATION_STATUS, name="notification_status"),
            nullable=False,
            server_default="UNREAD",
        ),
        # NULL = the whole tenant's inbox. Phase 8 never sets it; the column exists so
        # addressing one agent later is a filter, not a redesign.
        sa.Column("recipient_user_id", postgresql.UUID(as_uuid=True)),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        # Idempotency: one notification of each type per handoff, ever.
        sa.UniqueConstraint("handoff_id", "type", name="uq_internal_notifications_handoff_type"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "handoff_id"],
            ["human_handoffs.tenant_id", "human_handoffs.id"],
            name="fk_internal_notifications_handoff_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_internal_notifications_conversation_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "lead_id"],
            ["leads.tenant_id", "leads.id"],
            name="fk_internal_notifications_lead_tenant",
        ),
    )
    op.create_index("ix_internal_notifications_tenant_id", "internal_notifications", ["tenant_id"])
    # The inbox query: unread first, newest first, for one tenant.
    op.create_index(
        "ix_internal_notifications_inbox",
        "internal_notifications",
        ["tenant_id", "status", "created_at"],
    )
    _enable_rls("internal_notifications")

    log.info(
        "human_handoffs and internal_notifications created with forced RLS "
        "and a partial unique index for one active handoff per conversation"
    )


def downgrade() -> None:
    _disable_rls("internal_notifications")
    op.drop_index("ix_internal_notifications_inbox", table_name="internal_notifications")
    op.drop_index("ix_internal_notifications_tenant_id", table_name="internal_notifications")
    op.drop_table("internal_notifications")
    for name in ("notification_status", "notification_type"):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)

    _disable_rls("human_handoffs")
    op.drop_index("ix_human_handoffs_tenant_created", table_name="human_handoffs")
    op.drop_index("ix_human_handoffs_tenant_assignee", table_name="human_handoffs")
    op.drop_index("ix_human_handoffs_tenant_status", table_name="human_handoffs")
    op.drop_index("uq_human_handoffs_active_conversation", table_name="human_handoffs")
    op.drop_constraint("uq_human_handoffs_id_tenant", "human_handoffs", type_="unique")
    op.drop_table("human_handoffs")
    for name in ("handoff_reason", "handoff_status"):
        sa.Enum(name=name).drop(op.get_bind(), checkfirst=True)
