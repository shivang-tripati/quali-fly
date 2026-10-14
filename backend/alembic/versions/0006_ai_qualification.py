"""ai qualification: ai_message_runs

Revision ID: 0006_ai_qualification
Revises: 0005_conversation_engine
Create Date: 2026-09-28

Adds one table recording one AI attempt against one inbound message, ENABLE + FORCE ROW
LEVEL SECURITY with the same tenant policy as the rest of the schema, so
`tests/test_meta_rls.py::test_every_tenant_scoped_table_has_forced_rls` covers it
automatically.

Idempotency
-----------
    UNIQUE (inbound_message_id)

is the whole concurrency story. Every path that could process a message twice - a
duplicated webhook delivery, a replayed `meta_webhook_events` row, two workers picking up
the same conversation - converges on an `INSERT ... ON CONFLICT DO NOTHING`, so exactly
one run is ever claimed. The losers read the existing run and stop. No advisory lock, no
"are you sure nothing is in flight" check, and no window between reading and writing where
a second worker could slip in. Charging a lead two AI replies because two workers raced is
not an acceptable failure mode, so the constraint - not application logic - is the
enforcement point.

Tenant-carrying foreign keys
---------------------------
    ai_message_runs.conversation_id    -> conversations(id, tenant_id)     CASCADE
    ai_message_runs.inbound_message_id -> messages(id, tenant_id)         CASCADE
    ai_message_runs.outbound_message_id-> messages(id, tenant_id)
    ai_message_runs.lead_id            -> leads(id, tenant_id)

Every reference carries `tenant_id`, so a correctly-tenant-scoped insert cannot attach
itself to another tenant's message even with RLS disabled. This is the invariant Phase 5
established on the existing tables, extended to the new one rather than left as a hole.

`messages` and `leads` need the supporting `UNIQUE (id, tenant_id)`. Phase 5's
migration added it for `leads`, `conversations`, and `meta_connections`; `messages` was
created afterwards by this same phase, so it does not have one yet. It is added and
dropped here, alongside the constraints that need it.

NULL parents
------------
`lead_id` and `outbound_message_id` are nullable and the constraints are MATCH SIMPLE, so
a NULL satisfies them. This is required, not incidental: a conversation may have no
`Lead` (someone messages a page without ever submitting an ad form), and `outbound_message_id`
is unknown until after the provider call and the send succeed. Neither case is an error.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006_ai_qualification"
down_revision: str | None = "0005_conversation_engine"
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


# (child column, parent table, on delete) for every reference that must not cross tenants.
# inbound_message_id and conversation_id CASCADE because their row has no meaning without
# the conversation it came from. lead_id and outbound_message_id are audit links that
# should survive; they are nullable and MATCH SIMPLE tolerates NULL.
_RUN_FKS = (
    ("conversation_id", "conversations", "CASCADE"),
    ("inbound_message_id", "messages", "CASCADE"),
    ("outbound_message_id", "messages", None),
    ("lead_id", "leads", None),
)

# The parents that need a UNIQUE (id, tenant_id) for the composite references above.
# `leads` and `conversations` already have one from 0005; `messages` does not, so this
# migration adds and removes it.
_PARENTS_ALREADY_UNIQUE = ("leads", "conversations")
_PARENT_NEEDS_UNIQUE = ("messages",)


def upgrade() -> None:
    for parent in _PARENT_NEEDS_UNIQUE:
        op.create_unique_constraint(f"uq_{parent}_id_tenant", parent, ["id", "tenant_id"])

    op.create_table(
        "ai_message_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("inbound_message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True)),
        sa.Column("outbound_message_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "status",
            sa.Enum(
                "IN_PROGRESS",
                "PROCESSED",
                "FAILED",
                "SKIPPED",
                name="ai_run_status",
            ),
            nullable=False,
            server_default="IN_PROGRESS",
        ),
        # Coarse category only. Provider error text can echo prompt content, and this
        # column is readable through the API, so it never holds a message.
        sa.Column("error_category", sa.String(length=64)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provider", sa.String(length=32)),
        sa.Column("model", sa.String(length=64)),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("completion_tokens", sa.Integer()),
        sa.Column("latency_ms", sa.Integer()),
        sa.Column("qualification_total", sa.Integer()),
        sa.Column("high_intent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "human_handoff_required", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("handoff_reason", sa.String(length=500)),
        sa.Column("ai_response", postgresql.JSONB()),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # The idempotency guarantee. See the module docstring.
        sa.UniqueConstraint("inbound_message_id", name="uq_ai_message_runs_inbound_message"),
    )

    for column, parent, on_delete in _RUN_FKS:
        op.create_foreign_key(
            f"fk_ai_message_runs_{column}_tenant",
            "ai_message_runs",
            parent,
            [column, "tenant_id"],
            ["id", "tenant_id"],
            ondelete=on_delete,
        )

    op.create_index("ix_ai_message_runs_tenant_id", "ai_message_runs", ["tenant_id"])
    op.create_index("ix_ai_message_runs_conversation_id", "ai_message_runs", ["conversation_id"])
    # Serves the one query the pipeline actually runs: unclaimed inbound messages for a
    # conversation. Partial, because claimed rows vastly outnumber unclaimed ones.
    op.create_index(
        "ix_ai_message_runs_pending",
        "ai_message_runs",
        ["tenant_id", "conversation_id"],
        postgresql_where=sa.text("status = 'IN_PROGRESS'"),
    )
    _enable_rls("ai_message_runs")

    log.info("ai_message_runs created with forced RLS and tenant-carrying foreign keys")


def downgrade() -> None:
    _disable_rls("ai_message_runs")
    op.drop_table("ai_message_runs")
    # The enum type is NOT owned by the table in the way a column default might suggest:
    # `drop_table` leaves it behind, and a second `upgrade` then fails with
    # `type "ai_run_status" already exists`. A downgrade that cannot be re-applied is not
    # a downgrade, so the type is dropped here explicitly.
    sa.Enum(name="ai_run_status").drop(op.get_bind(), checkfirst=True)
    for parent in _PARENT_NEEDS_UNIQUE:
        op.drop_constraint(f"uq_{parent}_id_tenant", parent, type_="unique")
    for parent in _PARENTS_ALREADY_UNIQUE:
        # Left in place by 0005; assert it is there so a downgrade that half-succeeded
        # fails loudly instead of leaving the schema in a state that looks healthy.
        log.debug("retaining uq_%s_id_tenant from 0005", parent)
