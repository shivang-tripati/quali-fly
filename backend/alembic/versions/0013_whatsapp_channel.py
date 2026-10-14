"""WhatsApp Business: the channel, and the phone number a send is addressed to.

Phase 10-O.3 adds WhatsApp as a third transport. The conversation engine, the AI pipeline,
follow-ups and handoffs are untouched; what did not exist was the *address*.

**Two enum values and two columns. That is the whole migration**, and each part is
load-bearing:

  * `channel` / `message_channel` gain `WHATSAPP`. A WhatsApp thread is a different thread
    from a Messenger one - different participant id, different send endpoint - so it is a
    different enum value rather than a flag on a row. `ADD VALUE` cannot drop a value, so
    the downgrade recreates the type; see the note there.

  * `meta_connections.whatsapp_phone_number_id` is the address a send is POSTed to.
    `whatsapp_business_account_id` already exists and is the *account*; it is not something
    you can send to. Without this column the connection has no send target and
    `conversations._credential_for` would have to refuse every WhatsApp send, which is
    exactly the Phase 10-O.0 shape for Instagram. It is therefore part of "can this
    connection send", not an optional setting.

  * `whatsapp_display_number` is the human-readable number (`+44 7700 900123`). Stored so
    Settings can say *which* number is connected without spending a Graph call on every
    page view, and so a tenant is not asked to recognise an opaque id. It is not a secret
    and it is written only by the server, after Meta confirms the number belongs to the
    account.

Neither column is writable through `POST`/`PATCH /meta/connections`. `phone_number_id` is a
trusted provider identifier, so it changes only through the verified selection endpoint,
which re-queries Meta first - the same rule Phase 10-O.2 applied to
`instagram_business_id`.

Not added, deliberately: templates, media, campaigns, analytics, billing. Phase 10-O.3's
scope is text messages in both directions, and a template-management subsystem would be a
larger feature wearing a migration's clothes.
"""

import sqlalchemy as sa
from alembic import op

revision = "0013_whatsapp_channel"
down_revision = "0012_tenant_control_plane"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # `ADD VALUE` is transactional from PostgreSQL 12 onwards, and the new value is not
    # used again inside this migration, so the default transaction-per-revision is fine.
    op.execute("ALTER TYPE channel ADD VALUE IF NOT EXISTS 'WHATSAPP'")
    op.execute("ALTER TYPE message_channel ADD VALUE IF NOT EXISTS 'WHATSAPP'")

    op.add_column(
        "meta_connections",
        sa.Column("whatsapp_phone_number_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "meta_connections",
        sa.Column("whatsapp_display_number", sa.String(length=32), nullable=True),
    )

    # One phone number per active workspace, matching the existing partial-unique pattern on
    # page_id / instagram_business_id / whatsapp_business_account_id. Without it two tenants
    # could both claim the same number and race to reply to the same customer's thread.
    op.create_index(
        "uq_meta_connections_active_whatsapp_phone_number_id",
        "meta_connections",
        ["whatsapp_phone_number_id"],
        unique=True,
        postgresql_where=sa.text("whatsapp_phone_number_id IS NOT NULL AND status = 'ACTIVE'"),
    )


def downgrade() -> None:
    op.drop_index("uq_meta_connections_active_whatsapp_phone_number_id", "meta_connections")
    op.drop_column("meta_connections", "whatsapp_display_number")
    op.drop_column("meta_connections", "whatsapp_phone_number_id")

    # PostgreSQL cannot remove an enum value, so the types are rebuilt without it. The
    # existing rows are all MESSENGER/INSTAGRAM, so the recreate cannot lose data - but the
    # conversion guard is kept because a row written after the upgrade could hold WHATSAPP,
    # and dropping such a row silently during a downgrade is the one outcome that would be
    # indefensible.
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (SELECT 1 FROM conversations WHERE channel::text = 'WHATSAPP') THEN
                RAISE EXCEPTION
                    'cannot downgrade: WhatsApp conversations exist. Remove them first.';
            END IF;
        END $$;
        """
    )
    op.execute("ALTER TYPE message_channel RENAME TO message_channel_with_whatsapp")
    op.execute("ALTER TYPE channel RENAME TO channel_with_whatsapp")
    # Both types carry the *channel* values; `message_direction` is a different enum and is
    # not touched here.
    op.execute("CREATE TYPE message_channel AS ENUM ('MESSENGER', 'INSTAGRAM')")
    op.execute("CREATE TYPE channel AS ENUM ('MESSENGER', 'INSTAGRAM')")
    op.execute(
        "ALTER TABLE messages ALTER COLUMN channel TYPE message_channel "
        "USING channel::text::message_channel"
    )
    op.execute(
        "ALTER TABLE conversations ALTER COLUMN channel TYPE channel USING channel::text::channel"
    )
    op.execute("DROP TYPE message_channel_with_whatsapp")
    op.execute("DROP TYPE channel_with_whatsapp")
