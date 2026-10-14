"""meta integration: connections and durable webhook inbox

Revision ID: 0003_meta_integration
Revises: 0002_identity_and_tenancy
Create Date: 2026-09-28

Creates `meta_connections` (the tenant's Meta setup and the routing keys for inbound
webhooks) and `meta_webhook_events` (the durable inbox).

Security model
--------------
Both tables are ENABLE + FORCE ROW LEVEL SECURITY, exactly like `tenant_memberships`.
Only `app.current_tenant` scopes them, so every event row carries its own `tenant_id`
and isolation never depends on the connection id alone.

The one exception is *source resolution*. A Meta webhook arrives before any tenant is
known, so it cannot set `app.current_tenant`. Rather than weakening RLS or giving the
application role a second privileged connection, the migration creates a single narrow
`SECURITY DEFINER` function that answers exactly one question:

    "which ACTIVE connection owns this Meta page / Instagram business id?"

It returns only `(connection_id, tenant_id, provider)`, is STABLE, pins its
`search_path`, and its EXECUTE is revoked from PUBLIC and granted to the application
role only. It cannot be used to list connections or read payloads: there is no
function that returns rows, only one that maps an external id to a tenant.

`SECURITY DEFINER` is effective here because the schema owner is the POSTGRES_USER
superuser, and superusers bypass RLS unconditionally - which is exactly why the
application itself must not run as that role.
"""

import logging
import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.core.config import get_settings

revision: str = "0003_meta_integration"
down_revision: str | None = "0002_identity_and_tenancy"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

log = logging.getLogger("alembic.runtime.migration")
_ROLE_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_$-]*")

CURRENT_TENANT = "app.current_tenant"
TENANT_MATCH = f"tenant_id = nullif(current_setting('{CURRENT_TENANT}', true), '')::uuid"

RESOLVER = "public.resolve_meta_connection"
VERIFIER = "public.verify_meta_webhook_token"

RESOLVER_SQL = """
CREATE OR REPLACE FUNCTION public.resolve_meta_connection(
    p_page_id text DEFAULT NULL,
    p_instagram_business_id text DEFAULT NULL,
    p_whatsapp_business_account_id text DEFAULT NULL
)
RETURNS TABLE (connection_id uuid, tenant_id uuid, provider text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT c.id, c.tenant_id, c.provider::text
    FROM public.meta_connections c
    WHERE c.status = 'ACTIVE'
      AND (
            (p_page_id IS NOT NULL AND c.page_id = p_page_id)
         OR (p_instagram_business_id IS NOT NULL
                 AND c.instagram_business_id = p_instagram_business_id)
         OR (p_whatsapp_business_account_id IS NOT NULL
                 AND c.whatsapp_business_account_id = p_whatsapp_business_account_id)
      )
    LIMIT 1
$$;
"""

# The verification token is compared *inside* the database, so the stored digest is
# never returned to the application. The caller learns only "matched" or "not
# matched", which prevents both a leak of the digest and an offline brute-force of a
# tenant's verification token.
VERIFIER_SQL = """
CREATE OR REPLACE FUNCTION public.verify_meta_webhook_token(
    p_connection_id uuid,
    p_token_hash text
)
RETURNS TABLE (connection_id uuid, tenant_id uuid, provider text)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $$
    SELECT c.id, c.tenant_id, c.provider::text
    FROM public.meta_connections c
    WHERE c.id = p_connection_id
      AND c.webhook_verify_token_hash = p_token_hash
      AND c.status = 'ACTIVE'
    LIMIT 1
$$;
"""


def _grant_resolver() -> None:
    """Lock the trusted lookups down to the unprivileged application role.

    PostgreSQL grants EXECUTE on new functions to PUBLIC, so the default is revoked.
    If no application role is configured (host development without Compose) the grants
    stay on PUBLIC and a warning is logged: revoking without a replacement would break
    the webhook path with no obvious cause.
    """
    app_db_role = get_settings().app_db_role
    connection = op.get_bind()
    if not app_db_role:
        log.warning(
            "APP_DB_ROLE is not set, so %s and %s are executable by PUBLIC. "
            "Set APP_DB_ROLE before running migrations outside local development.",
            RESOLVER,
            VERIFIER,
        )
        return
    if not _ROLE_NAME.fullmatch(app_db_role):
        raise ValueError(f"APP_DB_ROLE is not a valid PostgreSQL identifier: {app_db_role!r}")
    for function in (RESOLVER, VERIFIER):
        connection.exec_driver_sql(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        connection.exec_driver_sql(f'GRANT EXECUTE ON FUNCTION {function} TO "{app_db_role}"')


def upgrade() -> None:
    op.create_table(
        "meta_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "provider",
            sa.Enum("META", name="meta_provider"),
            nullable=False,
            server_default="META",
        ),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "INACTIVE", name="meta_connection_status"),
            nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column("display_name", sa.String(200)),
        sa.Column("page_id", sa.String(64)),
        sa.Column("instagram_business_id", sa.String(64)),
        sa.Column("whatsapp_business_account_id", sa.String(64)),
        sa.Column("access_token_encrypted", sa.String(512)),
        sa.Column("token_expires_at", sa.DateTime(timezone=True)),
        sa.Column("app_id", sa.String(64)),
        sa.Column("webhook_verify_token_hash", sa.String(64)),
        sa.Column("last_webhook_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_meta_connections_tenant_id", "meta_connections", ["tenant_id"])
    op.create_index("ix_meta_connections_page_id", "meta_connections", ["page_id"])
    op.create_index(
        "ix_meta_connections_instagram_business_id",
        "meta_connections",
        ["instagram_business_id"],
    )

    # A Meta external identifier must never map to two active tenants, otherwise a
    # webhook could not be routed. Partial, so an identifier can be re-used once the
    # previous connection is INACTIVE.
    op.create_index(
        "uq_meta_connections_active_page_id",
        "meta_connections",
        ["page_id"],
        unique=True,
        postgresql_where=sa.text("page_id IS NOT NULL AND status = 'ACTIVE'"),
    )
    op.create_index(
        "uq_meta_connections_active_instagram_business_id",
        "meta_connections",
        ["instagram_business_id"],
        unique=True,
        postgresql_where=sa.text("instagram_business_id IS NOT NULL AND status = 'ACTIVE'"),
    )
    op.create_index(
        "uq_meta_connections_active_whatsapp_business_account_id",
        "meta_connections",
        ["whatsapp_business_account_id"],
        unique=True,
        postgresql_where=sa.text("whatsapp_business_account_id IS NOT NULL AND status = 'ACTIVE'"),
    )

    op.create_table(
        "meta_webhook_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("meta_connection_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("deduplication_key", sa.String(255), nullable=False),
        sa.Column("meta_event_id", sa.String(255)),
        sa.Column("object_type", sa.String(64)),
        sa.Column("event_type", sa.String(128)),
        sa.Column("source_identifier", sa.String(64)),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "headers", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default="{}"
        ),
        sa.Column(
            "status",
            sa.Enum(
                "RECEIVED",
                "PROCESSING",
                "PROCESSED",
                "FAILED",
                name="meta_webhook_event_status",
            ),
            nullable=False,
            server_default="RECEIVED",
        ),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("error_message", sa.Text()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["meta_connection_id"], ["meta_connections.id"], ondelete="CASCADE"
        ),
    )
    # The final, race-safe idempotency guard.
    op.create_index(
        "uq_meta_webhook_events_dedupe",
        "meta_webhook_events",
        ["meta_connection_id", "deduplication_key"],
        unique=True,
    )
    op.create_index("ix_meta_webhook_events_tenant_id", "meta_webhook_events", ["tenant_id"])
    op.create_index(
        "ix_meta_webhook_events_status_received_at",
        "meta_webhook_events",
        ["status", "received_at"],
    )

    # -- row level security ------------------------------------------------
    for table in ("meta_connections", "meta_webhook_events"):
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"CREATE POLICY {table}_select ON {table} FOR SELECT USING ({TENANT_MATCH})")
        op.execute(
            f"CREATE POLICY {table}_insert ON {table} FOR INSERT WITH CHECK ({TENANT_MATCH})"
        )
        op.execute(
            f"CREATE POLICY {table}_update ON {table} "
            f"FOR UPDATE USING ({TENANT_MATCH}) WITH CHECK ({TENANT_MATCH})"
        )
        op.execute(f"CREATE POLICY {table}_delete ON {table} FOR DELETE USING ({TENANT_MATCH})")

    op.execute(RESOLVER_SQL)
    op.execute(VERIFIER_SQL)
    _grant_resolver()


def downgrade() -> None:
    op.execute(f"DROP FUNCTION IF EXISTS {VERIFIER}")
    op.execute(f"DROP FUNCTION IF EXISTS {RESOLVER}")

    for table in ("meta_webhook_events", "meta_connections"):
        for suffix in ("delete", "update", "insert", "select"):
            op.execute(f"DROP POLICY IF EXISTS {table}_{suffix} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.drop_index("ix_meta_webhook_events_status_received_at", table_name="meta_webhook_events")
    op.drop_index("ix_meta_webhook_events_tenant_id", table_name="meta_webhook_events")
    op.drop_index("uq_meta_webhook_events_dedupe", table_name="meta_webhook_events")
    op.drop_table("meta_webhook_events")

    op.drop_table("meta_connections")
    sa.Enum(name="meta_webhook_event_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="meta_connection_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="meta_provider").drop(op.get_bind(), checkfirst=True)
