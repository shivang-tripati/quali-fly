"""identity and tenancy with RLS

Revision ID: 0002_identity_and_tenancy
Revises: 0001_enable_pgvector
Create Date: 2026-09-27

Phase 1 creates the identity/tenancy foundation only: tenants, users,
tenant_memberships, sessions, and the Row Level Security boundary.

Security model
--------------
`tenants`, `users` and `sessions` are NOT RLS-scoped: the tenant is the isolation
root, and identity lookups must work before any tenant is known. Access to them is
application-controlled and always keyed off the authenticated principal.

`tenant_memberships` is RLS-scoped and the policy is the union of two checks:

  * `app.current_user`   -> a user may always see their own memberships
                            (required to resolve which tenants they belong to)
  * `app.current_tenant` -> anyone inside a tenant may see that tenant's members

Both settings are applied with `SET LOCAL`, so they are reset at transaction end and
cannot leak across pooled connections. When neither is set, every policy evaluates to
NULL and no rows are visible: the default is deny.

`FORCE ROW LEVEL SECURITY` is required because the application connects as the table
owner, and owners bypass RLS unless it is forced.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_identity_and_tenancy"
down_revision: str | None = "0001_enable_pgvector"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CURRENT_TENANT = "app.current_tenant"
CURRENT_USER = "app.current_user"


def upgrade() -> None:
    # -- identity ---------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("password_hash", sa.String(255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("email", name="uq_users_email"),
        sa.CheckConstraint("email = lower(email)", name="ck_users_email_lowercase"),
    )
    op.create_index("ix_users_email", "users", ["email"])

    op.create_table(
        "tenants",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("slug", sa.String(63), nullable=False),
        sa.Column(
            "status",
            sa.Enum("ACTIVE", "SUSPENDED", name="tenant_status"),
            nullable=False,
            server_default="ACTIVE",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("slug", name="uq_tenants_slug"),
    )
    op.create_index("ix_tenants_slug", "tenants", ["slug"])
    op.create_index("ix_tenants_status", "tenants", ["status"])

    op.create_table(
        "tenant_memberships",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "role",
            sa.Enum("OWNER", "ADMIN", "MEMBER", name="membership_role"),
            nullable=False,
            server_default="MEMBER",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("tenant_id", "user_id", name="uq_tenant_memberships_tenant_user"),
    )
    op.create_index("ix_tenant_memberships_tenant_id", "tenant_memberships", ["tenant_id"])
    op.create_index("ix_tenant_memberships_user_id", "tenant_memberships", ["user_id"])

    # Opaque session tokens, stored hashed so a database leak yields no usable cookie.
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("token_hash", name="uq_sessions_token_hash"),
    )
    op.create_index("ix_sessions_user_id", "sessions", ["user_id"])

    # -- row level security ------------------------------------------------
    op.execute("ALTER TABLE tenant_memberships ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_memberships FORCE ROW LEVEL SECURITY")

    # `nullif(..., '')` matters: once a custom GUC has been used on a connection,
    # PostgreSQL resets it to an empty string rather than unsetting it, and `''::uuid`
    # would raise on the next request that reuses that pooled connection.
    tenant_match = f"tenant_id = nullif(current_setting('{CURRENT_TENANT}', true), '')::uuid"
    user_match = f"user_id = nullif(current_setting('{CURRENT_USER}', true), '')::uuid"

    op.execute(
        f"""
        CREATE POLICY tenant_memberships_select ON tenant_memberships
            FOR SELECT USING ({tenant_match} OR {user_match})
        """
    )
    op.execute(
        f"""
        CREATE POLICY tenant_memberships_insert ON tenant_memberships
            FOR INSERT WITH CHECK ({tenant_match})
        """
    )
    op.execute(
        f"""
        CREATE POLICY tenant_memberships_update ON tenant_memberships
            FOR UPDATE USING ({tenant_match}) WITH CHECK ({tenant_match})
        """
    )
    op.execute(
        f"""
        CREATE POLICY tenant_memberships_delete ON tenant_memberships
            FOR DELETE USING ({tenant_match})
        """
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS tenant_memberships_delete ON tenant_memberships")
    op.execute("DROP POLICY IF EXISTS tenant_memberships_update ON tenant_memberships")
    op.execute("DROP POLICY IF EXISTS tenant_memberships_insert ON tenant_memberships")
    op.execute("DROP POLICY IF EXISTS tenant_memberships_select ON tenant_memberships")
    op.execute("ALTER TABLE tenant_memberships NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE tenant_memberships DISABLE ROW LEVEL SECURITY")

    op.drop_table("sessions")
    op.drop_index("ix_tenant_memberships_user_id", table_name="tenant_memberships")
    op.drop_index("ix_tenant_memberships_tenant_id", table_name="tenant_memberships")
    op.drop_table("tenant_memberships")
    op.drop_index("ix_tenants_status", table_name="tenants")
    op.drop_index("ix_tenants_slug", table_name="tenants")
    op.drop_table("tenants")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
    sa.Enum(name="membership_role").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="tenant_status").drop(op.get_bind(), checkfirst=True)
