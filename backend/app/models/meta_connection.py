"""Tenant-scoped Meta integration connections.

One row per tenant's Meta app/page setup. Facebook, Instagram, Messenger and WhatsApp
are *surfaces* of the same connection, so they share this table and are distinguished
by the external identifiers Meta puts in webhook payloads.

The external identifiers are what route an inbound webhook to a tenant, so
`page_id` / `instagram_business_id` / `whatsapp_business_account_id` are constrained
to be unique across ACTIVE connections (partial unique indexes in migration 0003).
`whatsapp_phone_number_id` joins them in migration 0013 for the same reason.
Ambiguous routing is therefore impossible.

A connection is either hand-configured (migration 0002/0003: identifiers plus a
verification token, no credential) or OAuth-authorized (Phase 3: an encrypted access
token). `access_token_encrypted` being null is the signal for "cannot call the Graph
API", and the lead processor treats it that way rather than failing obscurely later.
"""

import enum
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class MetaProvider(enum.StrEnum):
    META = "META"


class MetaConnectionStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class MetaConnection(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "meta_connections"
    __table_args__ = (
        Index("ix_meta_connections_tenant_id", "tenant_id"),
        Index("ix_meta_connections_page_id", "page_id"),
        Index("ix_meta_connections_instagram_business_id", "instagram_business_id"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider: Mapped[MetaProvider] = mapped_column(
        SAEnum(MetaProvider, name="meta_provider", native_enum=True),
        nullable=False,
        default=MetaProvider.META,
    )
    status: Mapped[MetaConnectionStatus] = mapped_column(
        SAEnum(MetaConnectionStatus, name="meta_connection_status", native_enum=True),
        nullable=False,
        default=MetaConnectionStatus.ACTIVE,
    )
    display_name: Mapped[str | None] = mapped_column(String(200))

    # -- external identifiers (the routing keys) -------------------------
    page_id: Mapped[str | None] = mapped_column(String(64))
    instagram_business_id: Mapped[str | None] = mapped_column(String(64))
    whatsapp_business_account_id: Mapped[str | None] = mapped_column(String(64))
    # Phase 10-O.3: the account is what webhooks arrive *on*; the phone number is what a
    # send is addressed *to*. WhatsApp's Cloud API posts to `/{phone_number_id}/messages`,
    # so without this column there is no send target and every outbound attempt is
    # unsendable. Written only by the verified selection endpoint, never by the generic
    # connection API - see migration 0013.
    whatsapp_phone_number_id: Mapped[str | None] = mapped_column(String(64))
    # For Settings to name the number rather than an opaque id, without a Graph call per
    # page view. Not a secret; set from Meta's answer at selection time.
    whatsapp_display_number: Mapped[str | None] = mapped_column(String(32))

    # -- credentials ------------------------------------------------------
    # AES-256-GCM ciphertext (see app/core/crypto.py), base64url of nonce+sealed. The
    # plaintext token exists only in memory between an OAuth exchange and the Graph
    # call that needs it. Widened from String(512) in migration 0004: a page token with
    # a wide grant set plus GCM overhead can exceed 512 characters.
    access_token_encrypted: Mapped[str | None] = mapped_column(String(1024))
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    app_id: Mapped[str | None] = mapped_column(String(64))
    # Granted scopes, so a support request can tell "Meta refused" from "we never
    # asked". Read-only metadata; never sent to the browser beyond a summary.
    scopes: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    # When an authorized credential was last stored, as opposed to when the row was
    # created. Null for a manually configured connection.
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # SHA-256 of the Meta verification token. The plaintext is never stored,
    # returned or logged, so a database dump cannot be used to verify webhooks.
    webhook_verify_token_hash: Mapped[str | None] = mapped_column(String(64))
    last_webhook_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
