"""follow-up engine: scheduled_tasks and tenant follow-up config

Revision ID: 0008_follow_up_engine
Revises: 0007_knowledge_base
Create Date: 2026-09-28

Introduces `scheduled_tasks`, the durable source of truth for follow-up and ghost
evaluation, and adds one JSONB column to `tenants` for per-tenant templates.

Why a table and not APScheduler jobs
-------------------------------------
APScheduler is a trigger. Its job list lives in process memory, so replacing a container
loses it. Every unit of follow-up business state is therefore a row here, and the poller
does nothing but find due rows and claim them. A container restart is then a non-event.

Idempotency
-----------
    UNIQUE (conversation_id, task_type, sequence_step)

One conversation can have at most one task per step, ever. That single constraint is what
makes scheduling idempotent under concurrency: two pollers racing to create Follow-Up #1
produce one row and one loser, with no read-then-write window for the race to land in. It
also means re-scheduling after a *completed* step is a no-op rather than a second message
to a real lead.

Leases, not mutexes
-------------------
A claimed task is `PROCESSING` with `locked_at`. If a worker dies mid-execute the row does
not hang forever: once `locked_at` is older than the lease, another worker may reclaim it.
`locked_by` is diagnostic only. There is no advisory lock, and no lock is ever held across
an AI or Meta call - claiming and executing are separate short transactions.

Indexes
-------
  * partial `(tenant_id, scheduled_at) WHERE status = 'PENDING'` - the poller's only hot
    query, kept proportional to outstanding work rather than table history.
  * partial `(locked_at) WHERE status = 'PROCESSING'` - lease recovery.

Tenant integrity
----------------
`scheduled_tasks.conversation_id` is a composite `(tenant_id, conversations.id)`
reference, the pattern Phases 2-6 established, so a correctly-tenant-scoped insert cannot
attach a task to another tenant's conversation. `lead_id` is nullable and the constraint is
MATCH SIMPLE, so a conversation with no lead is normal rather than an error.

Rollback
--------
`downgrade` drops the enum types explicitly. `drop_table` leaves a native enum behind, and
the next `upgrade` then fails with `type "task_status" already exists" - the same trap
migration 0006 hit, so it is handled rather than rediscovered.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008_follow_up_engine"
down_revision: str | None = "0007_knowledge_base"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")

TENANT_MATCH = "tenant_id = nullif(current_setting('app.current_tenant', true), '')::uuid"


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
    # -- tenants: follow-up templates ------------------------------------
    # One column, not a table. The PRD's config is read whole and written whole, so there
    # are no rows to reference. `{}` means "use the built-in defaults", so every existing
    # tenant is functional without a backfill.
    op.add_column(
        "tenants",
        sa.Column(
            "follow_up_config",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )

    # -- scheduled_tasks --------------------------------------------------
    op.create_table(
        "scheduled_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "task_type",
            sa.Enum("FOLLOW_UP", "GHOST", name="task_type"),
            nullable=False,
        ),
        sa.Column("sequence_step", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "PROCESSING",
                "COMPLETED",
                "FAILED",
                "CANCELLED",
                name="task_status",
            ),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        # Lease, not a mutex. `locked_at` is the expiry basis.
        sa.Column("locked_at", sa.DateTime(timezone=True)),
        sa.Column("locked_by", sa.String(length=128)),
        # A short category, never a provider or Meta payload: scheduler logs are read by
        # operators and a provider error can echo prompt or message content.
        sa.Column("last_error", sa.String(length=64)),
        sa.Column("sent_text", sa.Text()),
        sa.Column("ai_enhanced", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("cancelled_at", sa.DateTime(timezone=True)),
        # The inbound timestamp the sequence is anchored to. The eligibility re-check
        # compares the conversation's `last_inbound_at` against this, which is how a reply
        # landing *after* the task was created invalidates it.
        sa.Column("activity_anchor_at", sa.DateTime(timezone=True)),
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
        sa.UniqueConstraint(
            "conversation_id",
            "task_type",
            "sequence_step",
            name="uq_scheduled_tasks_conversation_type_step",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "conversation_id"],
            ["conversations.tenant_id", "conversations.id"],
            name="fk_scheduled_tasks_conversation_tenant",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "lead_id"],
            ["leads.tenant_id", "leads.id"],
            name="fk_scheduled_tasks_lead_tenant",
        ),
    )

    op.create_index("ix_scheduled_tasks_scheduled_at", "scheduled_tasks", ["scheduled_at"])
    op.create_index("ix_scheduled_tasks_status", "scheduled_tasks", ["status"])
    op.create_index("ix_scheduled_tasks_tenant_id", "scheduled_tasks", ["tenant_id"])
    # Partial, so both of the scheduler's queries scan outstanding work only.
    op.create_index(
        "ix_scheduled_tasks_due",
        "scheduled_tasks",
        ["tenant_id", "scheduled_at"],
        postgresql_where=sa.text("status = 'PENDING'"),
    )
    op.create_index(
        "ix_scheduled_tasks_lease",
        "scheduled_tasks",
        ["locked_at"],
        postgresql_where=sa.text("status = 'PROCESSING'"),
    )
    _enable_rls("scheduled_tasks")

    log.info("scheduled_tasks created with forced RLS; tenants.follow_up_config added")


def downgrade() -> None:
    _disable_rls("scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_lease", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_due", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_tenant_id", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_status", table_name="scheduled_tasks")
    op.drop_index("ix_scheduled_tasks_scheduled_at", table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
    sa.Enum(name="task_type").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="task_status").drop(op.get_bind(), checkfirst=True)
    op.drop_column("tenants", "follow_up_config")
