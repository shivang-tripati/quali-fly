"""conversation engine: conversations and messages

Revision ID: 0005_conversation_engine
Revises: 0004_meta_lead_engine
Create Date: 2026-09-28

Introduces `conversations` (one thread per person per surface) and `messages` (one turn in
it). Both are ENABLE + FORCE ROW LEVEL SECURITY with the same tenant policy as the rest of
the schema, so `tests/test_meta_rls.py::test_every_tenant_scoped_table_has_forced_rls`
covers them automatically.

Deduplication
-------------
Two independent guards, because two independent retried things arrive from Meta:

    messages   UNIQUE (conversation_id, direction, meta_message_id)   one row per mid
    (Phase 3) leads / (Phase 2) meta_webhook_events                    already guarded

A `SELECT`-then-`INSERT` would let two concurrent deliveries of the same mid both
insert, so the unique index is the authority and the repositories use
`ON CONFLICT DO NOTHING`. Postgres treats NULLs as distinct, so several PENDING outbound
messages (no Meta id yet) do not collide with each other.

Delete behaviour
----------------
Deliberately asymmetric, and it is the important decision here:

  * `conversations.lead_id` and `conversations.meta_connection_id` are ON DELETE SET NULL
  * `messages.conversation_id` is ON DELETE CASCADE

A conversation is business data: disconnecting a Meta connection sets the connection to
INACTIVE and must not erase what people said to the tenant. A message, on the other hand,
has no meaning detached from its conversation, and conversations are never themselves
deleted - so CASCADE there is safe and keeps a hypothetical future hard delete from
leaving orphaned text.

Tenant-carrying foreign keys
---------------------------
This migration also replaces several single-column foreign keys with composite ones that
carry `tenant_id`, because RLS alone does not constrain *which parent* a row may point
at. Verified against PostgreSQL before this change: with the tenant context set to
Globex, a row could be inserted into `messages` carrying Globex's own `tenant_id` - so it
passed every RLS policy - while referencing **Acme's** conversation. The tenant's
isolation is only as good as the least constrained write path, and a plain application
bug or a future code path would have produced cross-tenant reads and writes with no
error anywhere.

So every parent reference in the tenant graph becomes `(id, tenant_id)`:

    messages.conversation_id      -> conversations(id, tenant_id)
    conversations.meta_connection_id -> meta_connections(id, tenant_id)
    conversations.lead_id         -> leads(id, tenant_id)
    leads.meta_connection_id      -> meta_connections(id, tenant_id)
    meta_webhook_events.meta_connection_id -> meta_connections(id, tenant_id)

The last two predate this phase; they are included because leaving them would mean the
fix applied only to the new tables while the same hole stayed open on the old ones.
Nullable parents (`leads.meta_connection_id`, which disconnect leaves NULL) are
unaffected: under MATCH SIMPLE a NULL column satisfies the constraint.
"""

import logging
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005_conversation_engine"
down_revision: str | None = "0004_meta_lead_engine"
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


# (child table, child column, parent table, on delete) for every parent reference that
# must not cross a tenant boundary.
_TENANT_FKS = (
    ("messages", "conversation_id", "conversations", "CASCADE"),
    ("conversations", "meta_connection_id", "meta_connections", "SET NULL"),
    ("conversations", "lead_id", "leads", "SET NULL"),
    ("leads", "meta_connection_id", "meta_connections", "SET NULL"),
    ("meta_webhook_events", "meta_connection_id", "meta_connections", "CASCADE"),
)


def _harden_tenant_foreign_keys() -> None:
    """Make it impossible for a tenant-owned row to reference another tenant's row.

    RLS scopes a row by its *own* `tenant_id`. It says nothing about the row a foreign
    key points at, so a single-column FK lets a correctly-tenant-scoped insert attach
    itself to another tenant's parent. Composite keys carry the tenant into the
    constraint itself, which is where the guarantee belongs.

    The supporting `UNIQUE (id, tenant_id)` constraints are technically redundant with
    each table's primary key. They exist because PostgreSQL requires the referenced
    columns to be covered by a unique constraint, and an index on an already-unique uuid
    is a small, cheap price for making the constraint possible.
    """
    for parent in ("conversations", "leads", "meta_connections"):
        op.create_unique_constraint(f"uq_{parent}_id_tenant", parent, ["id", "tenant_id"])

    for child, column, parent, on_delete in _TENANT_FKS:
        op.drop_constraint(f"{child}_{column}_fkey", child, type_="foreignkey", schema="public")
        op.create_foreign_key(
            f"fk_{child}_{column}_tenant",
            child,
            parent,
            [column, "tenant_id"],
            ["id", "tenant_id"],
            ondelete=on_delete,
        )


def _restore_single_column_foreign_keys() -> None:
    for child, column, parent, on_delete in _TENANT_FKS:
        op.drop_constraint(f"fk_{child}_{column}_tenant", child, type_="foreignkey")
        op.create_foreign_key(
            f"{child}_{column}_fkey",
            child,
            parent,
            [column],
            ["id"],
            ondelete=on_delete,
        )
    for parent in ("meta_connections", "leads", "conversations"):
        op.drop_constraint(f"uq_{parent}_id_tenant", parent, type_="unique")


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("meta_connection_id", postgresql.UUID(as_uuid=True)),
        sa.Column("lead_id", postgresql.UUID(as_uuid=True)),
        sa.Column(
            "channel",
            sa.Enum("MESSENGER", "INSTAGRAM", name="channel"),
            nullable=False,
        ),
        sa.Column("meta_participant_id", sa.String(64), nullable=False),
        sa.Column("meta_source_id", sa.String(64)),
        sa.Column("meta_referral_ad_id", sa.String(64)),
        sa.Column("last_message_at", sa.DateTime(timezone=True)),
        # The 24-hour messaging window is measured from this.
        sa.Column("last_inbound_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # SET NULL, not CASCADE: disconnecting a connection keeps the thread.
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["meta_connection_id"], ["meta_connections.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(["lead_id"], ["leads.id"], ondelete="SET NULL"),
    )
    # The thread identity: Meta's messaging webhook has no thread id, so the participant
    # plus the surface is the conversation. Also the race-safe find-or-create guard.
    op.create_index(
        "uq_conversations_thread",
        "conversations",
        ["meta_connection_id", "channel", "meta_participant_id"],
        unique=True,
    )
    op.create_index("ix_conversations_tenant_id", "conversations", ["tenant_id"])
    op.create_index(
        "ix_conversations_tenant_last_message",
        "conversations",
        ["tenant_id", "last_message_at"],
    )
    op.create_index("ix_conversations_lead_id", "conversations", ["lead_id"])
    _enable_rls("conversations")

    op.create_table(
        "messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "channel",
            sa.Enum("MESSENGER", "INSTAGRAM", name="message_channel"),
            nullable=False,
        ),
        sa.Column(
            "direction",
            sa.Enum("INBOUND", "OUTBOUND", name="message_direction"),
            nullable=False,
        ),
        sa.Column(
            "delivery",
            sa.Enum("PENDING", "SENT", "FAILED", name="message_delivery"),
            nullable=False,
            server_default="SENT",
        ),
        sa.Column("meta_message_id", sa.String(255)),
        sa.Column("message_type", sa.String(32), nullable=False, server_default="text"),
        sa.Column("text", sa.Text()),
        sa.Column(
            "attachments",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("failure_reason", sa.String(64)),
        # Phase 5 writes these; Phase 4 has no AI output to attribute.
        sa.Column("ai_model", sa.String(64)),
        sa.Column("ai_prompt_tokens", sa.Integer()),
        sa.Column("ai_completion_tokens", sa.Integer()),
        sa.Column("ai_latency_ms", sa.Integer()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
    )
    # Inbound dedupe. NULL meta_message_id values do not collide, so multiple PENDING
    # outbound rows are fine.
    op.create_index(
        "uq_messages_dedupe",
        "messages",
        ["conversation_id", "direction", "meta_message_id"],
        unique=True,
    )
    op.create_index("ix_messages_tenant_id", "messages", ["tenant_id"])
    op.create_index("ix_messages_conversation_sent_at", "messages", ["conversation_id", "sent_at"])
    _enable_rls("messages")

    # Last, so the rows above are already consistent when the constraints land.
    _harden_tenant_foreign_keys()


def downgrade() -> None:
    _restore_single_column_foreign_keys()

    _disable_rls("messages")
    op.drop_index("ix_messages_conversation_sent_at", table_name="messages")
    op.drop_index("ix_messages_tenant_id", table_name="messages")
    op.drop_index("uq_messages_dedupe", table_name="messages")
    op.drop_table("messages")
    sa.Enum(name="message_delivery").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="message_direction").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="message_channel").drop(op.get_bind(), checkfirst=True)

    _disable_rls("conversations")
    op.drop_index("ix_conversations_lead_id", table_name="conversations")
    op.drop_index("ix_conversations_tenant_last_message", table_name="conversations")
    op.drop_index("ix_conversations_tenant_id", table_name="conversations")
    op.drop_index("uq_conversations_thread", table_name="conversations")
    op.drop_table("conversations")
    sa.Enum(name="channel").drop(op.get_bind(), checkfirst=True)
