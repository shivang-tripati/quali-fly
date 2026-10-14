"""WhatsApp Business setup state, and the verified choice of a sending number.

Phase 10-O.3 needed a tenant to be able to say *which* number QualiFly sends from, without
being able to assert it. The shape follows Phase 10-O.2's Instagram work exactly, for the
same reason: a phone number id is a trusted provider identifier that a send is addressed
to, so accepting one from a request body would let a tenant point outbound messages at
someone else's number.

So the flow is the same three steps and for the same reasons:

    discover  ->  ask Meta which numbers the account owns
    select    ->  re-ask Meta, store only a match
    report    ->  say honestly what is and is not working

**The state vocabulary is deliberately multi-valued, not a boolean.** The failure Phase
10-O.0 found repeated here is a control plane that says "configured" because some columns
are populated. A WhatsApp connection can be:

    not_connected     there is no active Meta connection to configure
    awaiting_account  a Meta connection exists, but no WhatsApp Business Account has been
                      bound to it - one step from being able to list numbers
    awaiting_number   an account is bound, but no number has been chosen - cannot send
    configured        a number is chosen and Meta-confirmed, but this deployment has no
                      usable Meta credential, so a send would fail
    ready             configured, on a connection that is active with a token

`platform_available` and `unavailable_reason` are reported alongside, matching the tri-state
`ai_status`/`follow_up_status` already established in `tenant_settings`. A tenant reading
"ready" is being told a send would be attempted against a live credential, which is exactly
the claim a boolean cannot make.

**No state here asserts that a real message was ever delivered.** A development or mock
connection reads as `configured` or `ready` on the strength of stored, verified identifiers
- it never reads as proof of provider connectivity, and nothing in the response implies one.
"""

import logging
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.core.crypto import decrypt
from app.models.meta_connection import MetaConnection, MetaConnectionStatus
from app.repositories import meta_connections as connection_repo
from app.services.meta_graph import (
    MetaApiError,
    MetaAuthError,
    MetaGraphClient,
    MetaTransientError,
)

log = logging.getLogger("app.meta.whatsapp")

STATE_NOT_CONNECTED = "not_connected"
STATE_AWAITING_ACCOUNT = "awaiting_account"
STATE_AWAITING_NUMBER = "awaiting_number"
STATE_CONFIGURED = "configured"
STATE_READY = "ready"

REASON_NO_META_CREDENTIAL = "no_meta_credential_configured"


class WhatsAppNotConfigurable(Exception):
    """The connection cannot carry a WhatsApp number, and the caller can fix it."""


class WhatsAppNumberNotFound(Exception):
    """The submitted phone number id is not one Meta reports for this account."""


class WhatsAppAccountNotFound(Exception):
    """Meta would not confirm the submitted WhatsApp Business Account for this connection."""


@dataclass(frozen=True, slots=True)
class WhatsAppNumberOption:
    """One selectable number, as offered to the tenant."""

    phone_number_id: str
    display_number: str | None
    verified_name: str | None
    is_verified: bool

    @property
    def label(self) -> str:
        parts = [self.verified_name, self.display_number]
        shown = " · ".join(p for p in parts if p)
        return shown or self.phone_number_id


def platform_whatsapp_available(settings: Settings) -> tuple[bool, str | None]:
    """Can this deployment call the WhatsApp API at all?

    The same rule as `tenant_settings.platform_ai_available`, for a transport rather than a
    model: a Meta app with credentials is what every Cloud API call needs, and
    `META_GRAPH_API_VERSION` must be one that serves the WhatsAPI version. Without either, no
    send can be attempted no matter what the tenant configured.
    """
    if not (settings.meta_app_id and settings.meta_app_secret):
        return False, REASON_NO_META_CREDENTIAL
    return True, None


def _waba_token(connection: MetaConnection, settings: Settings) -> str:
    """The token and account a discovery call needs, or the precise reason it cannot."""
    if connection.status is not MetaConnectionStatus.ACTIVE:
        raise WhatsAppNotConfigurable(
            "This Meta connection is not active. Reconnect Meta before choosing a WhatsApp number."
        )
    if not connection.whatsapp_business_account_id:
        raise WhatsAppNotConfigurable(
            "This Meta connection has no WhatsApp Business Account. Add one to the "
            "connection before choosing a number."
        )
    if not connection.access_token_encrypted:
        raise WhatsAppNotConfigurable(
            "This Meta connection has no Meta credential. Reconnect Meta before choosing "
            "a WhatsApp number."
        )
    return decrypt(connection.access_token_encrypted, key=settings.meta_token_encryption_key)


async def discover_numbers(
    *,
    connection: MetaConnection,
    settings: Settings,
    client: MetaGraphClient | None = None,
) -> list[WhatsAppNumberOption]:
    """Ask Meta which phone numbers this WhatsApp Business Account owns.

    An empty list is a fact the settings screen explains, not an error.
    """
    token = _waba_token(connection, settings)
    owned = client is None
    graph = client or MetaGraphClient(
        app_id=settings.meta_app_id,
        app_secret=settings.meta_app_secret,
        graph_version=settings.meta_graph_api_version,
        timeout_seconds=settings.meta_graph_timeout_seconds,
    )
    try:
        found = await graph.list_whatsapp_phone_numbers(
            waba_id=connection.whatsapp_business_account_id or "",
            access_token=token,
        )
    except MetaAuthError:
        log.warning(
            "meta_whatsapp_discovery_failed",
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
        WhatsAppNumberOption(
            phone_number_id=number.phone_number_id,
            display_number=number.display_number,
            verified_name=number.verified_name,
            is_verified=number.is_verified,
        )
        for number in found
    ]
    log.info(
        "meta_whatsapp_discovered",
        extra={
            "meta_connection_id": str(connection.id),
            "tenant_id": str(connection.tenant_id),
            "candidate_count": len(options),
        },
    )
    return options


async def associate_business_account(
    session: AsyncSession,
    *,
    connection: MetaConnection,
    whatsapp_business_account_id: str,
    settings: Settings,
    client: MetaGraphClient | None = None,
) -> MetaConnection:
    """Bind a WhatsApp Business Account to this connection, after Meta confirms it.

    The same rule as `select_number`, one level up: the browser proposes an id, Meta
    decides. Without this step the account id would be a trusted value taken from a request
    body, and a tenant could bind an account they do not own - after which every webhook for
    that account would route into this workspace and every reply would be sent as though
    from someone else's number.

    Re-binding clears any previously chosen number: the number belonged to the old account,
    and leaving it would advertise a send address that no longer belongs here.
    """
    requested = whatsapp_business_account_id.strip()
    if not requested:
        raise WhatsAppAccountNotFound(
            "Enter the WhatsApp Business Account id from Meta, or disconnect the account "
            "first to stop WhatsApp on this connection."
        )
    if not connection.access_token_encrypted:
        raise WhatsAppNotConfigurable(
            "This Meta connection has no Meta credential. Reconnect Meta before choosing "
            "a WhatsApp Business Account."
        )
    token = decrypt(connection.access_token_encrypted, key=settings.meta_token_encryption_key)

    owned = client is None
    graph = client or MetaGraphClient(
        app_id=settings.meta_app_id,
        app_secret=settings.meta_app_secret,
        graph_version=settings.meta_graph_api_version,
        timeout_seconds=settings.meta_graph_timeout_seconds,
    )
    try:
        account = await graph.get_whatsapp_business_account(waba_id=requested, access_token=token)
    except (MetaTransientError, MetaAuthError):
        # Deliberately *not* folded into the "Meta did not confirm" refusal below. A timeout
        # and a revoked token are not statements about the tenant's id, and reporting them as
        # "check your id" would send them looking for a typo in an id that was right all
        # along. The endpoint maps these to 503/502 with something they can act on.
        log.warning(
            "meta_whatsapp_account_check_failed",
            extra={
                "meta_connection_id": str(connection.id),
                "tenant_id": str(connection.tenant_id),
                "reason": "unavailable",
            },
        )
        raise
    except MetaApiError as exc:
        # One refusal for every reason, and the account's name is never echoed: a
        # difference in message would turn this into an oracle for guessing ids.
        log.warning(
            "meta_whatsapp_account_rejected",
            extra={
                "meta_connection_id": str(connection.id),
                "tenant_id": str(connection.tenant_id),
                "reason": type(exc).__name__,
            },
        )
        raise WhatsAppAccountNotFound(
            "Meta did not confirm that WhatsApp Business Account for this connection. "
            "Check the id in Meta, and that this Meta account can see it."
        ) from exc
    finally:
        if owned:
            await graph.aclose()

    if account.waba_id != requested:
        # Meta answered for a different node than the one asked about. Treating that as
        # success would bind an id the caller never chose.
        raise WhatsAppAccountNotFound(
            "Meta answered for a different WhatsApp Business Account than the one given."
        )

    connection = await connection_repo.update_connection(
        session,
        connection,
        {
            "whatsapp_business_account_id": account.waba_id,
            "whatsapp_phone_number_id": None,
            "whatsapp_display_number": None,
        },
    )
    log.info(
        "meta_whatsapp_account_selected",
        extra={
            "meta_connection_id": str(connection.id),
            "tenant_id": str(connection.tenant_id),
            "whatsapp_business_account_id": account.waba_id,
        },
    )
    return connection


async def select_number(
    session: AsyncSession,
    *,
    connection: MetaConnection,
    phone_number_id: str,
    settings: Settings,
    client: MetaGraphClient | None = None,
) -> MetaConnection:
    """Bind a sending number to this connection, after Meta confirms the account owns it.

    A fresh discovery call rather than a check against what the browser was shown, so an id
    the server never offered - or one belonging to another account or tenant - cannot be
    written. The display number is stored alongside the id so Settings can name it without
    a Graph call, and so a tenant is not asked to recognise an opaque identifier.
    """
    requested = phone_number_id.strip()
    options = await discover_numbers(connection=connection, settings=settings, client=client)
    if not options:
        raise WhatsAppNumberNotFound(
            "Meta reports no phone number on this WhatsApp Business Account. Add one in "
            "Meta, then choose it here."
        )
    match = next((o for o in options if o.phone_number_id == requested), None)
    if match is None:
        # The available ids are not echoed back: they are the tenant's own data, but an
        # error naming them turns this endpoint into a way to enumerate them.
        raise WhatsAppNumberNotFound(
            "That phone number is not registered on this WhatsApp Business Account. "
            "Choose one of the numbers listed here."
        )

    connection = await connection_repo.update_connection(
        session,
        connection,
        {
            "whatsapp_phone_number_id": match.phone_number_id,
            "whatsapp_display_number": match.display_number,
        },
    )
    log.info(
        "meta_whatsapp_number_selected",
        extra={
            "meta_connection_id": str(connection.id),
            "tenant_id": str(connection.tenant_id),
            "phone_number_id": match.phone_number_id,
        },
    )
    return connection


def setup_state(connection: MetaConnection | None, settings: Settings) -> dict[str, object]:
    """What the tenant can honestly be told about WhatsApp on this connection.

    The states are explained at the module level. Two boundaries matter here.

    `not_connected` versus `awaiting_account`: the first means there is no Meta connection to
    configure, the second means there is one and WhatsApp is not finished. They are different
    instructions to the tenant - "connect Meta" and "add your account id" - and collapsing
    them into one state leaves the tenant with nothing to act on.

    And `configured` versus `ready`: a stored, Meta-confirmed number says the tenant has
    finished setup, and *not* that this deployment can currently reach Meta.
    """
    platform_available, reason = platform_whatsapp_available(settings)

    if connection is None or connection.status is not MetaConnectionStatus.ACTIVE:
        state = STATE_NOT_CONNECTED
    elif not connection.whatsapp_business_account_id:
        state = STATE_AWAITING_ACCOUNT
    elif not connection.whatsapp_phone_number_id:
        state = STATE_AWAITING_NUMBER
    elif connection.access_token_encrypted:
        state = STATE_READY if platform_available else STATE_CONFIGURED
    else:
        # A number is chosen but the connection cannot be called - the credential was
        # cleared. Not "ready", and not "not connected" either.
        state = STATE_CONFIGURED

    return {
        "state": state,
        "ready": state == STATE_READY,
        "whatsapp_business_account_id": (
            connection.whatsapp_business_account_id if connection else None
        ),
        "phone_number_id": connection.whatsapp_phone_number_id if connection else None,
        "display_number": connection.whatsapp_display_number if connection else None,
        "platform_available": platform_available,
        "unavailable_reason": reason,
    }


__all__ = [
    "STATE_AWAITING_ACCOUNT",
    "STATE_AWAITING_NUMBER",
    "STATE_CONFIGURED",
    "STATE_NOT_CONNECTED",
    "STATE_READY",
    "WhatsAppAccountNotFound",
    "WhatsAppNotConfigurable",
    "WhatsAppNumberNotFound",
    "WhatsAppNumberOption",
    "associate_business_account",
    "discover_numbers",
    "platform_whatsapp_available",
    "select_number",
    "setup_state",
]
