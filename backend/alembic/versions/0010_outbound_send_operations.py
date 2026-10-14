"""outbound_send_operations: attribute an outbound message to the task that produced it.

Revision ID: 0010_outbound_send_operations
Revises: 0009_human_handoff

Why this exists
---------------
Phase 7 records a follow-up's *intent* on `scheduled_tasks` and its *outcome* on `messages`,
but nothing joined the two. That gap is only cosmetic until a worker dies in the window
between them:

    worker claims task  ->  Meta accepts the message  ->  worker dies

The message is durably `SENT`. The task is still `PROCESSING`, its lease expires, and the
replacement worker has no way to know the lead was already messaged - so it sends again.
The local database is the only place that can answer "did this task already go out?", and
it could not because the two records were unrelated.

`messages.scheduled_task_id` joins them. Nullable, because a human's reply and the AI's own
first response are not produced by a task, and because adding a NOT NULL column to a table
that already holds data would rewrite every existing message for no benefit.

Uniqueness is the actual safety property, and it is partial on purpose:

    UNIQUE (scheduled_task_id) WHERE scheduled_task_id IS NOT NULL
                      AND direction = 'OUTBOUND'
                      AND delivery = 'SENT'

One *delivered* outbound per task, ever. A second delivered send for the same task cannot be
*recorded*, not merely discouraged - so a regression in the service layer surfaces as a
constraint violation rather than a second message to a real prospect.

Only `SENT` is constrained, and that is the whole point of the predicate:

* `PENDING` is an attempt recorded before the call whose outcome was never seen. A worker that
  died there must be free to try again, so several PENDING rows per task are normal.
* `FAILED` is a known non-delivery, and a bounded retry legitimately records several of them
  before one succeeds.

Constraining either of those would make a legitimate retry impossible at the database, which
is a worse failure than the duplicate it was meant to prevent.

RLS
---
`messages` already has RLS enabled and forced (migration 0005), and this migration adds no
new policy: the new column is covered by the existing row policy, which is keyed on
`tenant_id`. The foreign key is declared with a composite tenant reference so a task from
another tenant cannot be attached to this tenant's message - the same composite-FK pattern
the other tenant-scoped references in this schema use.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_outbound_send_operations"
down_revision = "0009_human_handoff"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # A composite unique on `(id, tenant_id)` on the referenced side. Postgres requires a
    # unique index over exactly the referenced columns, and `scheduled_tasks` only has the
    # primary key on `id` plus the sequence-uniqueness constraint. This is the same shape
    # `messages` already carries (`uq_messages_id_tenant`), so the pattern is established
    # rather than invented here.
    op.create_unique_constraint(
        "uq_scheduled_tasks_id_tenant", "scheduled_tasks", ["id", "tenant_id"]
    )

    op.add_column(
        "messages",
        sa.Column("scheduled_task_id", postgresql.UUID(as_uuid=True), nullable=True),
    )

    # Composite tenant FK: the referenced pair must be the task that shares this message's
    # tenant. A plain FK on `scheduled_tasks.id` would let a message point at another
    # tenant's task, which is exactly the kind of cross-tenant reference RLS does not cover.
    op.create_foreign_key(
        "fk_messages_scheduled_task_tenant",
        "messages",
        "scheduled_tasks",
        ["tenant_id", "scheduled_task_id"],
        ["tenant_id", "id"],
        ondelete="CASCADE",
    )

    op.create_index(
        "uq_messages_task_outbound_sent",
        "messages",
        ["scheduled_task_id"],
        unique=True,
        postgresql_where=sa.text(
            "scheduled_task_id IS NOT NULL AND direction = 'OUTBOUND' AND delivery = 'SENT'"
        ),
    )

    # Supports the recovery read: "has this task already produced a message?" scoped by
    # task, which is the hot path when a crashed worker's task is reclaimed.
    op.create_index(
        "ix_messages_scheduled_task_id",
        "messages",
        ["scheduled_task_id"],
        postgresql_where=sa.text("scheduled_task_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_messages_scheduled_task_id", table_name="messages")
    op.drop_index("uq_messages_task_outbound_sent", table_name="messages")
    op.drop_constraint("fk_messages_scheduled_task_tenant", "messages", type_="foreignkey")
    op.drop_column("messages", "scheduled_task_id")
    op.drop_constraint("uq_scheduled_tasks_id_tenant", "scheduled_tasks", type_="unique")
