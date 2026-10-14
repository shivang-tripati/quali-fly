"""tenant_control_plane: let a tenant own its business identity and its on/off switches.

Revision ID: 0012_tenant_control_plane
Revises: 0011_event_claim_ownership

Why this exists
---------------
Phase 10-O.0 audited what a brand-new tenant could actually configure and found almost
nothing. Three findings drove this migration, and all three were "the data structure exists
but nobody can write it":

1. The AI was never told who it works for. `RagContextProvider` never set
   `business_description`, so `BusinessContext.render()` always emitted the hardcoded
   "No service or product detail has been configured for this business." A tenant had no
   column, no API and no field to put their business in.

2. `tenants.follow_up_config` (added in Phase 7) is validated, rendered and consumed by the
   scheduler - and had no writer anywhere in the application. The only code that had ever
   written it was `tests/test_follow_ups.py`.

3. AI and follow-ups were global env flags. A tenant could not turn its own automation off
   without a platform-wide restart that affected every other tenant on the deployment.

What this adds
--------------
Four nullable-or-defaulted columns on `tenants`:

    business_name          TEXT NULL     -> falls back to `tenants.name`
    business_description   TEXT NULL     -> no fallback; the prompt handles absence
    ai_enabled             BOOL NOT NULL DEFAULT true
    follow_up_enabled      BOOL NOT NULL DEFAULT true

**Why `ai_enabled` / `follow_up_enabled` default to `true` and not `false`.** They are not
the deployment gate - `AI_ENABLED` and `FOLLOW_UP_ENABLED` already are, and they stay the
master switches. These columns mean "this tenant wants this", so the safe default for a row
that predates this migration is "yes, behave as you did before". Defaulting them to `false`
would silently switch off every existing tenant's automation on upgrade, which is the single
worst outcome this migration could have.

The effective state is therefore `platform AND tenant`, and both halves are reported
separately to the client so "the deployment cannot do this" is never shown as "you turned it
off".

RLS
---
`tenants` is the isolation root and is deliberately **not** RLS-scoped: it holds the row
every policy keys on, and access to it is always decided by `TenantMembership`, which is.
The two new descriptive columns are no different - they are readable only through endpoints
that already require a verified membership, exactly like `tenants.name` is today.

No table is added here. Team onboarding (Phase 10-O.1 §6) deliberately reuses `users` +
`tenant_memberships` with an admin-created member and a one-time password, rather than a
new invitations table plus a public token endpoint, because there is no email delivery in
this codebase to deliver an invitation with and a token endpoint would be a second
unauthenticated surface for no additional security.

Rollback
--------
Drops the four columns. The data is lost, which is why nothing here is required: a tenant
that had set a business description returns to the previous prompt behaviour, and the two
booleans return to following the platform flag alone.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0012_tenant_control_plane"
down_revision = "0011_event_claim_ownership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable, because there is no honest default. Inventing "Acme Ltd sells things" for
    # every existing tenant would put fabricated business facts in front of a model, and the
    # prompt already has a correct sentence for "this tenant has not told us yet".
    op.add_column("tenants", sa.Column("business_name", sa.String(200), nullable=True))
    op.add_column("tenants", sa.Column("business_description", sa.Text(), nullable=True))

    # server_default so the upgrade is a constant-time operation on a table that already
    # holds production rows, and so the backfill value is visible in the schema rather than
    # only in application code.
    op.add_column(
        "tenants",
        sa.Column(
            "ai_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "tenants",
        sa.Column(
            "follow_up_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column("tenants", "follow_up_enabled")
    op.drop_column("tenants", "ai_enabled")
    op.drop_column("tenants", "business_description")
    op.drop_column("tenants", "business_name")
