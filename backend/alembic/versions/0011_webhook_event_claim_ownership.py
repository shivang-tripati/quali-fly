"""webhook_event_claim_ownership: let a crashed worker's event be recovered, and stop a
stale worker from overwriting a newer one's result.

Revision ID: 0011_event_claim_ownership
Revises: 0010_outbound_send_operations

The identifier is 26 characters because `alembic_version.version_num` is `varchar(32)`.
A longer name fails at insert time rather than at revision time, which is an unpleasant way
to find out - the first symptom is a truncation error from the test suite's migration step.

Why this exists
---------------
`claim_next_received` moved an event `RECEIVED -> PROCESSING` under `FOR UPDATE SKIP
LOCKED`, but the row recorded neither *who* claimed it nor *when*. Three real gaps followed,
and all three are silent:

    1. A worker that died mid-processing left the row PROCESSING forever. Nothing ever
       selected a PROCESSING row, so the event was unreachable by every future worker. A
       lead that arrived during a deploy could be silently dropped forever.

    2. `mark_processed` and `mark_failed` filtered on `id` alone. Once any mechanism
       reclaims an expired claim, a worker whose lease lapsed mid-flight could still write
       its own result over the new owner's - turning a delivered lead into a FAILED event
       that retries forever, or (worse) marking an event PROCESSED whose business effect
       had failed, so the failure is never retried at all.

    3. Nothing counted attempts, so "bounded retries" was a property of nothing. A
       permanently-broken event classified retryable would loop indefinitely.

The shape is deliberately identical to `scheduled_tasks` (`locked_by` / `locked_at` /
`attempt_count`), because the follow-up worker already solves the identical problem and a
second, different claim scheme in the same schema would be one more thing to reason about.

What is *not* changed
--------------------
The dedupe boundary is untouched. `uq_meta_webhook_events_dedupe` on
`(meta_connection_id, deduplication_key)` remains the authority for "is this the same
delivery", and the `ON CONFLICT DO NOTHING` in `insert_if_new` remains the only write on
the delivery path. This migration adds nothing to the *ingest* side; it only makes the
*processing* side recoverable and guarded.

Backfill
--------
All three columns are nullable or have a default, so existing rows need no data migration:

  * `locked_by` / `locked_at` NULL on an existing PROCESSING row means "claimed by a worker
    that no longer exists, at a time we cannot know". Treating NULL as *expired* is the
    safe reading - it recovers such rows immediately rather than stranding them. That
    costs at most one redundant re-processing of an event that was already in flight when
    the previous version was deployed, and every downstream effect is idempotent, so the
    cost is bounded and known.
  * `attempt_count` 0 for existing rows, which treats them as never-attempted. Requeue
    therefore allows a normal budget of retries for them.

RLS
---
`meta_webhook_events` already has RLS enabled and forced (migration 0003). No policy is
added or changed: these columns are covered by the existing row policy keyed on
`tenant_id`, and the claim is only ever written after `app.current_tenant` is set.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_event_claim_ownership"
down_revision = "0010_outbound_send_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meta_webhook_events",
        sa.Column("locked_by", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "meta_webhook_events",
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "meta_webhook_events",
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )

    # Supports the reclaim sweep: "PROCESSING rows whose lease has lapsed", which is
    # exactly the query a worker runs when it wants something to do and a crash
    # investigation runs when it wants to know what was stranded.
    op.create_index(
        "ix_meta_webhook_events_status_locked_at",
        "meta_webhook_events",
        ["status", "locked_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_meta_webhook_events_status_locked_at", table_name="meta_webhook_events")
    op.drop_column("meta_webhook_events", "attempt_count")
    op.drop_column("meta_webhook_events", "locked_at")
    op.drop_column("meta_webhook_events", "locked_by")
