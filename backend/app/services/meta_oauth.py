"""Meta OAuth: authorize, then turn an authorized user into a usable connection.

The security shape, in order:

  1. start is a normal tenant-admin endpoint. The tenant comes from the authenticated
     `TenantContext`, never from a query parameter.
  2. a random, single-use, short-lived state row records (tenant, user, redirect). Only
     its digest is stored.
  3. the callback is public. It recovers the tenant *from the state row*, validates
     nothing from the URL, and consumes the state atomically so it cannot be replayed.
  4. the code is exchanged server-to-server, where the app secret lives.
  5. the page access token is encrypted before it reaches the database and is never
     returned, logged, or included in an exception.

Token lifetime: Meta's page tokens issued through `/me/accounts` are what lead
retrieval needs, and they are the long-lived kind. `token_expires_at` is only populated
when Meta actually reports an expiry, so a null expiry means "Meta did not say", not
"never expires" - see the README.
"""

import logging
import uuid
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config import Settings
from app.core.crypto import encrypt
from app.db.tenant_context import set_current_tenant
from app.models.meta_connection import MetaConnection, MetaConnectionStatus
from app.repositories import meta_connections as connection_repo
from app.repositories import meta_oauth_states as state_repo
from app.services.meta_graph import (
    MetaApiError,
    MetaAuthError,
    MetaGraphClient,
    MetaTransientError,
)

log = logging.getLogger("app.meta.oauth")


class OAuthNotConfigured(Exception):
    """The deployment has no Meta app id / secret / redirect URI."""


class OAuthStateRejected(Exception):
    """The state was unknown, already used, or expired. One failure, no detail."""


class NoAuthorizedPage(Exception):
    """The user authorized us but no Page came back we can use for lead retrieval."""


def _require_oauth_config(settings: Settings) -> None:
    if not (settings.meta_app_id and settings.meta_app_secret and settings.meta_oauth_redirect_uri):
        raise OAuthNotConfigured(
            "META_APP_ID, META_APP_SECRET and META_OAUTH_REDIRECT_URI must all be set."
        )


def build_graph_client(settings: Settings) -> MetaGraphClient:
    return MetaGraphClient(
        app_id=settings.meta_app_id,
        app_secret=settings.meta_app_secret,
        graph_version=settings.meta_graph_api_version,
        timeout_seconds=settings.meta_graph_timeout_seconds,
    )


async def start_oauth(
    session: AsyncSession,
    *,
    settings: Settings,
    tenant_id: uuid.UUID,
    user_id: uuid.UUID,
) -> tuple[str, str]:
    """Create state and return `(raw_state, authorize_url)`.

    The raw state is returned to the browser exactly once, inside the redirect to Meta.
    It is not logged and it is not stored.
    """
    _require_oauth_config(settings)
    client = build_graph_client(settings)
    try:
        raw_state, state = await state_repo.create_state(
            session,
            tenant_id=tenant_id,
            user_id=user_id,
            redirect_uri=settings.meta_oauth_redirect_uri,  # type: ignore[arg-type]
            scopes=settings.meta_oauth_scopes,
            ttl_seconds=settings.meta_oauth_state_ttl_seconds,
        )
    except IntegrityError:
        # A 32-byte random state colliding is not a real possibility; if it somehow
        # happens, refusing beats retrying forever.
        log.error("meta_oauth_state_collision", extra={"tenant_id": str(tenant_id)})
        raise OAuthStateRejected("Could not start the Meta connection flow.") from None

    url = client.authorization_url(
        redirect_uri=settings.meta_oauth_redirect_uri,  # type: ignore[arg-type]
        state=raw_state,
        scopes=settings.meta_oauth_scopes,
    )
    log.info(
        "meta_oauth_started",
        extra={
            "tenant_id": str(tenant_id),
            "state_id": str(state.id),
            "expires_at": state.expires_at.isoformat(),
            "graph_version": settings.meta_graph_api_version,
        },
    )
    return raw_state, url


async def complete_oauth(
    session: AsyncSession,
    *,
    state_factory: async_sessionmaker,
    settings: Settings,
    code: str,
    state: str,
    expected_user_id: uuid.UUID | None = None,
) -> MetaConnection:
    """Validate state, exchange the code, and store an authorized connection.

    `state_factory` opens the short transaction that burns the state. It is separate from
    `session` on purpose: the rest of this function can fail, and a rolled-back
    consumption would leave the state reusable - which is precisely the replay the state
    exists to prevent.

    `expected_user_id` is set when the caller happens to have an authenticated session.
    The tenant always comes from the state row; a caller that *does* have a session and
    is not the user who started the flow is rejected, so a link cannot be completed by
    someone else.
    """
    _require_oauth_config(settings)

    consumed = await state_repo.consume_state_in_own_transaction(state_factory, state)
    if consumed is None:
        log.warning("meta_oauth_state_rejected", extra={"reason": "invalid_state"})
        raise OAuthStateRejected("The Meta connection request is no longer valid.")
    if expected_user_id is not None and consumed.user_id != expected_user_id:
        # Consuming first means a state is burned even on this rejection, so it cannot
        # be probed repeatedly.
        log.warning(
            "meta_oauth_state_rejected",
            extra={"reason": "user_mismatch", "tenant_id": str(consumed.tenant_id)},
        )
        raise OAuthStateRejected("The Meta connection request is no longer valid.")

    # The state row was the trusted pre-tenant read. Now that it has been validated, open
    # the tenant scope for the rest of the transaction - the same sequence `ingest()`
    # uses after resolving a webhook source. Without this the INSERT into
    # `meta_connections` is rejected by its own RLS policy.
    await set_current_tenant(session, consumed.tenant_id)

    client = build_graph_client(settings)
    try:
        exchange = await client.exchange_authorization_code(
            code=code, redirect_uri=consumed.redirect_uri
        )
    except MetaAuthError:
        log.warning("meta_oauth_exchange_failed", extra={"reason": "rejected_by_meta"})
        raise
    except MetaTransientError:
        log.warning("meta_oauth_exchange_failed", extra={"reason": "meta_unavailable"})
        raise
    except MetaApiError as exc:
        log.warning("meta_oauth_exchange_failed", extra={"reason": "unexpected"})
        raise exc

    pages = await client.list_authorized_pages(exchange.access_token)
    if not pages:
        log.warning("meta_oauth_no_pages", extra={"tenant_id": str(consumed.tenant_id)})
        raise NoAuthorizedPage(
            "No Facebook Page with lead access was shared. Grant access to a Page that "
            "runs lead ads, then try again."
        )

    return await _store_authorized_connection(
        session,
        settings=settings,
        tenant_id=consumed.tenant_id,
        page=pages[0],
        extra_pages=pages[1:],
        expires_at=(
            datetime.fromtimestamp(exchange.expires_at_epoch, tz=UTC)
            if exchange.expires_at_epoch
            else None
        ),
    )


async def _store_authorized_connection(
    session: AsyncSession,
    *,
    settings: Settings,
    tenant_id: uuid.UUID,
    page,
    extra_pages: list,
    expires_at: datetime | None,
) -> MetaConnection:
    """Create or refresh the connection for the authorized Page.

    The first page wins because a Meta app is usually tied to one business's Page. The
    rest are logged, not silently dropped: connecting several Pages per tenant is a
    product decision, and guessing at it here would hide a real question.
    """
    connection = await connection_repo.find_active_by_page_id(session, page.page_id)

    values = {
        "page_id": page.page_id,
        "display_name": page.name,
        "app_id": settings.meta_app_id,
        "status": MetaConnectionStatus.ACTIVE,
        # Encrypt before the value is anywhere but this function's stack frame.
        "access_token_encrypted": encrypt(
            page.access_token, key=settings.meta_token_encryption_key
        ),
        "token_expires_at": expires_at,
        "scopes": sorted(_scope_list(settings)),
        "connected_at": datetime.now(UTC),
    }

    if connection is None:
        try:
            connection = await connection_repo.create_connection(
                session, tenant_id=tenant_id, values=values
            )
        except IntegrityError:
            # Another active connection already claims this page id.
            log.warning(
                "meta_oauth_page_conflict",
                extra={"tenant_id": str(tenant_id), "page_id": page.page_id},
            )
            raise
        log.info(
            "meta_connection_authorized",
            extra={"tenant_id": str(tenant_id), "meta_connection_id": str(connection.id)},
        )
    else:
        # Re-authorizing refreshes the credential in place. The connection id, and so
        # the webhook routing and the tenant's leads, are preserved.
        connection = await connection_repo.update_connection(session, connection, values)
        log.info(
            "meta_connection_reauthorized",
            extra={
                "tenant_id": str(tenant_id),
                "meta_connection_id": str(connection.id),
                "page_id": page.page_id,
            },
        )

    if extra_pages:
        log.info(
            "meta_oauth_extra_pages_ignored",
            extra={"tenant_id": str(tenant_id), "extra_page_count": len(extra_pages)},
        )
    return connection


def _scope_list(settings: Settings) -> list[str]:
    return [s for s in settings.meta_oauth_scopes.replace(" ", "").split(",") if s]


async def disconnect(session: AsyncSession, connection: MetaConnection) -> MetaConnection:
    """Stop using a connection without losing the tenant's history.

    Status goes INACTIVE, which is what `resolve_meta_connection` and the lead processor
    both check, and the encrypted credential is cleared so a disconnected connection
    cannot call the Graph API even if some future code path forgets the status check.
    Leads are untouched: `leads.meta_connection_id` is ON DELETE SET NULL, and
    disconnecting is not a delete.
    """
    connection = await connection_repo.update_connection(
        session,
        connection,
        {
            "status": MetaConnectionStatus.INACTIVE,
            "access_token_encrypted": None,
            "token_expires_at": None,
        },
    )
    log.info(
        "meta_connection_disconnected",
        extra={
            "meta_connection_id": str(connection.id),
            "tenant_id": str(connection.tenant_id),
        },
    )
    return connection


__all__ = [
    "MetaApiError",
    "MetaAuthError",
    "MetaTransientError",
    "NoAuthorizedPage",
    "OAuthNotConfigured",
    "OAuthStateRejected",
    "build_graph_client",
    "complete_oauth",
    "disconnect",
    "start_oauth",
]
