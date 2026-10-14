"""Short-lived OAuth state: create on the authenticated path, consume on the public one.

The split matters. `create_state` runs with a real `TenantContext` (the admin clicked
"Connect Meta"), so it is an ordinary tenant-scoped insert. `consume_state` runs from
the callback, where there is no session and no tenant, so it goes through the
`SECURITY DEFINER` function from migration 0004 rather than a plain SELECT.

Only a digest of the state value is stored, so a database dump cannot be replayed as a
callback, and the raw value never touches the database at all.
"""

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models.meta_oauth_state import MetaOAuthState

log = logging.getLogger("app.meta.oauth")

# 32 bytes of entropy, base64url: a state is a CSRF token, so guessing it must be
# infeasible even though it also functions as a lookup key.
_STATE_BYTES = 32

_CONSUME_SQL = text(
    """
    SELECT tenant_id, user_id, redirect_uri
    FROM consume_meta_oauth_state(:state_hash)
    """
)


@dataclass(frozen=True, slots=True)
class ConsumedState:
    """What a valid, unused, unexpired state yields. Everything else yields nothing."""

    tenant_id: uuid.UUID
    user_id: uuid.UUID
    redirect_uri: str


def new_state_value() -> str:
    return secrets.token_urlsafe(_STATE_BYTES)


def hash_state(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


async def create_state(
    session: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
    redirect_uri: str,
    scopes: str | None,
    ttl_seconds: int,
) -> tuple[str, MetaOAuthState]:
    """Persist a new state. Returns the raw value, which is never stored.

    The caller puts the raw value in the redirect to Meta and nothing else. It is
    returned here and nowhere else, so the database copy is a digest from the moment it
    is written.
    """
    raw_state = new_state_value()
    state = MetaOAuthState(
        tenant_id=tenant_id,
        user_id=user_id,
        state_hash=hash_state(raw_state),
        redirect_uri=redirect_uri,
        requested_scopes=scopes,
        expires_at=datetime.now(UTC) + timedelta(seconds=ttl_seconds),
    )
    session.add(state)
    await session.flush()
    return raw_state, state


async def consume_state(session: AsyncSession, raw_state: str) -> ConsumedState | None:
    """Atomically claim a state, returning `None` if it is unknown, used or expired.

    The `UPDATE ... WHERE consumed_at IS NULL` inside the database function is the
    single-use guarantee. Two concurrent callbacks with the same state therefore cannot
    both succeed, and the caller cannot tell the three rejection reasons apart.
    """
    row = (await session.execute(_CONSUME_SQL, {"state_hash": hash_state(raw_state)})).one_or_none()
    if row is None:
        return None

    state = ConsumedState(
        tenant_id=row.tenant_id,
        user_id=row.user_id,
        redirect_uri=row.redirect_uri,
    )
    # Tenant and user ids are not secret, and the state hash is useless once consumed,
    # so this is safe to log and genuinely useful for diagnosing a bad flow.
    log.info(
        "meta_oauth_state_consumed",
        extra={"tenant_id": str(state.tenant_id), "user_id": str(state.user_id)},
    )
    return state


async def consume_state_in_own_transaction(
    factory: async_sessionmaker, raw_state: str
) -> ConsumedState | None:
    """Consume a state in a short transaction that commits on its own.

    This exists because the OAuth callback's *request* transaction rolls back whenever
    the flow fails - Meta rejects the code, the user authorized no usable Page - and a
    rolled-back consumption would hand the very same state back for another attempt.
    Single-use has to survive failure, so burning the state is committed before any
    other work begins.
    """
    async with factory() as session, session.begin():
        return await consume_state(session, raw_state)


async def delete_expired(session: AsyncSession, *, now: datetime | None = None) -> int:
    """Housekeeping. Runs opportunistically; expiry is already enforced in SQL."""
    cutoff = now or datetime.now(UTC)
    result = await session.execute(
        select(MetaOAuthState.id).where(MetaOAuthState.expires_at < cutoff).limit(1000)
    )
    stale = list(result.scalars().all())
    if stale:
        from sqlalchemy import delete

        await session.execute(delete(MetaOAuthState).where(MetaOAuthState.id.in_(stale)))
    return len(stale)


__all__ = [
    "ConsumedState",
    "consume_state",
    "consume_state_in_own_transaction",
    "create_state",
    "delete_expired",
    "hash_state",
    "new_state_value",
]
