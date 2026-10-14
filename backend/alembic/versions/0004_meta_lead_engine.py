"""lead engine: leads and short-lived OAuth state

Revision ID: 0004_meta_lead_engine
Revises: 0003_meta_integration
Create Date: 2026-09-28

Introduces the first business entity, `leads`, plus the server-side OAuth state that
makes the Meta callback safe.

`leads`
--------
`UNIQUE (tenant_id, meta_leadgen_id)` is the PRD's deduplication rule and the reason
this phase exists. It is enforced by the database rather than by a `SELECT`-then-
`INSERT`, because Meta retries webhooks and we replay events by hand: two concurrent
processors must not both decide the lead is new. Every path to a lead row - webhook,
replay, retry - goes through the same unique index.

`meta_oauth_states`
-------------------
The OAuth callback is public: Meta redirects the browser to it, and that request may
carry neither a session nor a trustworthy tenant id. The tenant and user that started
the flow therefore come from a row, not from the URL.

Consuming that row is a read-then-write that two concurrent callbacks would both win,
so it is one `SECURITY DEFINER` function doing `UPDATE ... WHERE consumed_at IS NULL
RETURNING`. It is a *consume*, not a listing: it returns exactly the one row matching
one unguessable digest, or nothing. `EXECUTE` is revoked from PUBLIC and granted to the
application role only, matching migration 0003.

Both tables are ENABLE + FORCE ROW LEVEL SECURITY with the same tenant policy as
`meta_connections`, so `tests/test_meta_rls.py::test_every_tenant_scoped_table_has_
forced_rls` covers them automatically.
"""

import logging
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.config import get_settings

revision: str = "0004_meta_lead_engine"
down_revision: str | None = "0003_meta_integration"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")
_ROLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$-]*")

TENANT_MATCH = "tenant_id = nullif(current_setting('app.current_tenant', true), '')::uuid"

STATE_CONSUMER = "public.consume_meta_oauth_state"

# Atomic single-use consumption. `consumed_at IS NULL` is the guard, and the UPDATE
# happens in one statement, so a second concurrent callback finds nothing and gets no
# row back. Returning nothing is also what an expired or unknown state produces: the
# caller must not be able to tell those apart.
STATE_CONSUMER_SQL = """
CREATE OR REPLACE FUNCTION public.consume_meta_oauth_state(p_state_hash text)
RETURNS TABLE (tenant_id uuid, user_id uuid, redirect_uri text)
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    UPDATE public.meta_oauth_states s
    SET consumed_at = now()
    WHERE s.state_hash = p_state_hash
      AND s.consumed_at IS NULL
      AND s.expires_at > now()
    RETURNING s.tenant_id, s.user_id, s.redirect_uri
$$;
"""


def _grant_state_consumer() -> None:
    """Same lockdown as migration 0003: revoke PUBLIC, grant the app role only."""
    app_db_role = get_settings().app_db_role
    connection = op.get_bind()
    if not app_db_role:
        log.warning(
            "APP_DB_ROLE is not set, so %s is executable by PUBLIC. "
            "Set APP_DB_ROLE before running migrations outside local development.",
            STATE_CONSUMER,
        )
        return
    if not _ROLE_NAME.fullmatch(app_db_role):
        raise ValueError(f"APP_DB_ROLE is not a valid PostgreSQL identifier: {app_db_role!r}")
    connection.exec_driver_sql(f"REVOKE ALL ON FUNCTION {STATE_CONSUMER} FROM PUBLIC")
    connection.exec_driver_sql(f'GRANT EXECUTE ON FUNCTION {STATE_CONSUMER} TO "{app_db_role}"')


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
    # -- leads ------------------------------------------------------------
    op.create_table(
        "leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("meta_connection_id", postgresql.UUID(as_uuid=True)),
        sa.Column("meta_leadgen_id", sa.String(64), nullable=False),
        sa.Column(
            "source",
            sa.Enum("META_LEAD_ADS", name="lead_source"),
            nullable=False,
            server_default="META_LEAD_ADS",
        ),
        sa.Column(
            "status",
            sa.Enum(
                "NEW",
                "REPLIED",
                "QUALIFIED",
                "CLOSED",
                "GHOSTED",
                "HUMAN_HANDOFF",
                name="lead_status",
            ),
            nullable=False,
            server_default="NEW",
        ),
        sa.Column("name", sa.String(255)),
        sa.Column("email", sa.String(320)),
        sa.Column("phone", sa.String(64)),
        sa.Column("form_id", sa.String(64)),
        sa.Column("ad_id", sa.String(64)),
        sa.Column("provider_created_at", sa.DateTime(timezone=True)),
        sa.Column(
            "field_data",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("qualification", postgresql.JSONB(astext_type=sa.Text())),
        sa.Column("ingestion_note", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        # SET NULL, not CASCADE: disconnecting a Meta connection must never take the
        # tenant's leads with it.
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["meta_connection_id"], ["meta_connections.id"], ondelete="SET NULL"
        ),
    )
    op.create_index(
        "uq_leads_tenant_meta_leadgen_id",
        "leads",
        ["tenant_id", "meta_leadgen_id"],
        unique=True,
    )
    op.create_index("ix_leads_tenant_id", "leads", ["tenant_id"])
    op.create_index("ix_leads_tenant_status", "leads", ["tenant_id", "status"])
    _enable_rls("leads")

    # -- oauth state ------------------------------------------------------
    op.create_table(
        "meta_oauth_states",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("state_hash", sa.String(64), nullable=False),
        sa.Column("redirect_uri", sa.String(1024), nullable=False),
        sa.Column("requested_scopes", sa.String(1024)),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "uq_meta_oauth_states_state_hash", "meta_oauth_states", ["state_hash"], unique=True
    )
    op.create_index("ix_meta_oauth_states_tenant_id", "meta_oauth_states", ["tenant_id"])
    op.create_index("ix_meta_oauth_states_expires_at", "meta_oauth_states", ["expires_at"])
    _enable_rls("meta_oauth_states")

    op.execute(STATE_CONSUMER_SQL)
    _grant_state_consumer()

    # -- meta_connections: credential metadata ---------------------------
    # A 512-char column truncates a base64url AES-GCM envelope built from a page token
    # with a wide grant set, which would fail the INSERT and lose the connection.
    op.alter_column(
        "meta_connections",
        "access_token_encrypted",
        type_=sa.String(1024),
        existing_type=sa.String(512),
    )
    op.add_column(
        "meta_connections",
        sa.Column(
            "scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="[]"
        ),
    )
    op.add_column("meta_connections", sa.Column("connected_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {STATE_CONSUMER}")

    op.drop_column("meta_connections", "connected_at")
    op.drop_column("meta_connections", "scopes")
    op.alter_column(
        "meta_connections",
        "access_token_encrypted",
        type_=sa.String(512),
        existing_type=sa.String(1024),
    )

    _disable_rls("meta_oauth_states")
    op.drop_index("ix_meta_oauth_states_expires_at", table_name="meta_oauth_states")
    op.drop_index("ix_meta_oauth_states_tenant_id", table_name="meta_oauth_states")
    op.drop_index("uq_meta_oauth_states_state_hash", table_name="meta_oauth_states")
    op.drop_table("meta_oauth_states")

    _disable_rls("leads")
    op.drop_index("ix_leads_tenant_status", table_name="leads")
    op.drop_index("ix_leads_tenant_id", table_name="leads")
    op.drop_index("uq_leads_tenant_meta_leadgen_id", table_name="leads")
    op.drop_table("leads")

    sa.Enum(name="lead_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="lead_source").drop(op.get_bind(), checkfirst=True)
