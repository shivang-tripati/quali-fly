"""Short-lived, single-use OAuth state.

The callback is a public endpoint: Meta redirects the browser there and the browser may
have lost the session, so the request carries nothing we can trust. Without a
server-side record of who started the flow, any tenant's connection could be completed
by any other tenant's callback. So the tenant and user are carried in the database and
recovered from here, never from a query parameter.

Only a SHA-256 digest of the state value is stored, so a database dump cannot be
replayed as a callback. The raw value exists only in the redirect URL Meta sends and in
the browser that receives it.

Consumption is atomic and happens in a `SECURITY DEFINER` function (migration 0004)
rather than a `SELECT`-then-`UPDATE`, because two concurrent callbacks carrying the same
state must not both succeed. The table is also RLS-protected like every other
tenant-owned table, so the authenticated start path is scoped normally.
"""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.mixins import TimestampMixin, UUIDPrimaryKeyMixin


class MetaOAuthState(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "meta_oauth_states"
    __table_args__ = (
        Index("uq_meta_oauth_states_state_hash", "state_hash", unique=True),
        Index("ix_meta_oauth_states_tenant_id", "tenant_id"),
        # The cleanup sweep for expired rows reads this.
        Index("ix_meta_oauth_states_expires_at", "expires_at"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
    )
    # The admin who started the flow. The callback re-checks this against the recorded
    # state, so a state cannot be completed by a different user.
    user_id: Mapped[uuid.UUID] = mapped_column(
        PGUUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    state_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    # The exact redirect URI used for the authorization request. Meta requires the
    # exchange to use the same value, so it has to survive the round trip.
    redirect_uri: Mapped[str] = mapped_column(String(1024), nullable=False)
    # What we asked for, kept for diagnosing a rejected grant.
    requested_scopes: Mapped[str | None] = mapped_column(String(1024))

    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Set by the consume function. NULL until then; the single-use guard.
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
