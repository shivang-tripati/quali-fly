"""Choosing which Instagram Business Account a Meta connection operates.

Phase 10-O.0's finding: `MetaConnection.instagram_business_id` existed, nothing ever wrote
it, and the only way to set it was a hand-run API call with a number the browser supplied.
This module is the supported path, and it is shaped around one rule:

    **The browser proposes, Meta disposes.** A tenant picks an id from a list this server
    produced; persisting that id re-queries Meta and stores nothing the query did not just
    return. A client that submits an id it was never offered, or one belonging to another
    Page or another tenant, is refused - so the column can only ever hold a value Meta
    confirmed is attached to the Page whose token the tenant holds.

Nothing here creates a credential store or an OAuth implementation. The Page access token is
read from the existing `MetaConnection.access_token_encrypted` via the existing `crypto`
helper and handed straight to the existing `MetaGraphClient`.

**Why Instagram needs the Page, and why that is a feature.** Meta models an Instagram
Business Account as an edge owned by a Page, and the existing outbound path
(`conversations._credential_for`, `MetaGraphClient.send_message`) refuses to send without a
`page_id`. Requiring a Page here means a correctly provisioned Instagram connection is
immediately sendable, with no change to messaging and no second send endpoint.
"""

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import decrypt
from app.models.meta_connection import MetaConnection, MetaConnectionStatus
from app.repositories import meta_connections as connection_repo
from app.services.meta_graph import MetaAuthError, MetaGraphClient

log = logging.getLogger("app.meta.instagram")


class InstagramNotSelectable(Exception):
    """This connection cannot have an Instagram account attached, and the caller can fix it."""


class InstagramAccountNotFound(Exception):
    """The submitted id is not among the accounts Meta reports for this Page."""


@dataclass(frozen=True, slots=True)
class InstagramAccountOption:
    """One selectable account, as offered to the tenant.

    Carries only what the person choosing needs to tell two accounts apart. The id is
    included because it is what the client sends back, and because a tenant comparing two
    accounts against Meta's own tooling needs it; it is never a credential.
    """

    instagram_business_id: str
    username: str | None
    name: str | None

    @property
    def label(self) -> str:
        """Human-readable name, falling back so two entries are never indistinguishable."""
        return self.name or self.username or f"Instagram {self.instagram_business_id}"


def _page_token(connection: MetaConnection, settings: Settings) -> str:
    """Decrypt the connection's Page token, or explain precisely what is missing.

    Each refusal names the missing thing rather than saying "not configured", because the
    three causes need three different fixes and this is the only place that can tell them
    apart. The decrypted token is returned and never stored, logged or put in an exception.
    """
    if connection.status is not MetaConnectionStatus.ACTIVE:
        raise InstagramNotSelectable(
            "This Meta connection is not active. Reconnect Meta before choosing an "
            "Instagram account."
        )
    if not connection.page_id:
        # Unreachable through OAuth, which always stores a Page. Reachable by a hand-made
        # POST /connections, and worth naming: an Instagram account is addressed through
        # its Page, so without one there is nothing to discover.
        raise InstagramNotSelectable(
            "This Meta connection has no Facebook Page, and an Instagram Business "
            "Account is attached to a Page. Reconnect Meta to choose one."
        )
    if not connection.access_token_encrypted:
        raise InstagramNotSelectable(
            "This Meta connection has no Meta credential. Reconnect Meta before choosing "
            "an Instagram account."
        )
    return decrypt(connection.access_token_encrypted, key=settings.meta_token_encryption_key)


async def discover_accounts(
    *,
    connection: MetaConnection,
    settings: Settings,
    client: MetaGraphClient | None = None,
) -> list[InstagramAccountOption]:
    """Ask Meta which Instagram Business Accounts this connection's Page actually has.

    Returns an empty list when the Page simply has none, which is a fact the settings
    screen explains rather than an error it hides behind a failed request.
    """
    token = _page_token(connection, settings)
    owned = client is None
    graph = client or MetaGraphClient(
        app_id=settings.meta_app_id,
        app_secret=settings.meta_app_secret,
        graph_version=settings.meta_graph_api_version,
        timeout_seconds=settings.meta_graph_timeout_seconds,
    )
    try:
        found = await graph.list_instagram_business_accounts(
            page_id=connection.page_id or "",  # _page_token proved it is set
            page_access_token=token,
        )
    except MetaAuthError:
        log.warning(
            "meta_instagram_discovery_failed",
            extra={
                "meta_connection_id": str(connection.id),
                "tenant_id": str(connection.tenant_id),
                "reason": "auth",
            },
        )
        raise
    finally:
        if owned:
            await graph.aclose()

    options = [
        InstagramAccountOption(
            instagram_business_id=account.business_id,
            username=account.username,
            name=account.name,
        )
        for account in found
    ]
    log.info(
        "meta_instagram_discovered",
        extra={
            "meta_connection_id": str(connection.id),
            "tenant_id": str(connection.tenant_id),
            "candidate_count": len(options),
        },
    )
    return options


async def select_account(
    session: AsyncSession,
    *,
    connection: MetaConnection,
    instagram_business_id: str,
    settings: Settings,
    client: MetaGraphClient | None = None,
) -> MetaConnection:
    """Bind `instagram_business_id` to this connection, after Meta confirms the link.

    The verification is a fresh discovery call rather than a check against what the browser
    was shown: the list is a hint that may be stale, this is the check. That is what makes
    three separate attacks fail - an arbitrary id, an id from another Page, and an id
    belonging to a Page another tenant holds - all with one comparison.
    """
    requested = instagram_business_id.strip()
    options = await discover_accounts(connection=connection, settings=settings, client=client)
    if not options:
        raise InstagramAccountNotFound(
            "Meta reports no Instagram Business Account on this Facebook Page. Link one "
            "to the Page in Meta, then choose it here."
        )
    match = next(
        (o for o in options if o.instagram_business_id == requested),
        None,
    )
    if match is None:
        # The ids offered are not echoed back: they are the tenant's own data, but a
        # message naming what *is* available is not what this endpoint is for, and an
        # error that quotes an id teaches a prober which ids exist.
        raise InstagramAccountNotFound(
            "That Instagram Business Account is not linked to this Facebook Page. "
            "Choose one of the accounts listed here."
        )

    connection = await connection_repo.update_connection(
        session, connection, {"instagram_business_id": match.instagram_business_id}
    )
    log.info(
        "meta_instagram_account_selected",
        extra={
            "meta_connection_id": str(connection.id),
            "tenant_id": str(connection.tenant_id),
            "instagram_business_id": match.instagram_business_id,
        },
    )
    return connection


__all__ = [
    "InstagramAccountNotFound",
    "InstagramAccountOption",
    "InstagramNotSelectable",
    "discover_accounts",
    "select_account",
]
