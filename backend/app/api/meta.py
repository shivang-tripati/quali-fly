"""Tenant admin API for Meta connections and the Meta OAuth flow.

Authenticated, tenant-scoped and RLS-protected like every other Phase 1 endpoint.
Mutations need ADMIN or OWNER; a MEMBER may read the tenant's connections but must not
be able to change credentials. Secrets are write-only.

The OAuth callback is the one deliberately anonymous endpoint here. Meta redirects the
browser to it, and that request may carry no session, so it recovers the tenant from a
server-side single-use state row rather than from the request. See
`app/services/meta_oauth.py`.
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.deps import (
    SESSION_COOKIE_NAME,
    CurrentUserSession,
    DbSession,
    get_current_principal,
    require_minimum_role,
)
from app.core.auth import Principal, TenantContext
from app.core.config import Settings, get_settings
from app.core.errors import ApiError, ErrorCode, error_responses
from app.db.session import get_session_factory
from app.models.membership import Role
from app.models.meta_webhook_event import MetaWebhookEvent
from app.repositories import meta_connections as connection_repo
from app.schemas.meta import (
    InstagramAccountOptionResponse,
    InstagramAccountsResponse,
    InstagramSelectRequest,
    MetaConnectionCreate,
    MetaConnectionResponse,
    MetaConnectionUpdate,
    OAuthCallbackResponse,
    OAuthStartResponse,
    WebhookEventResponse,
    WhatsAppAccountSelectRequest,
    WhatsAppNumberOptionResponse,
    WhatsAppNumberSelectRequest,
    WhatsAppNumbersResponse,
)
from app.services import meta_instagram, meta_oauth, meta_whatsapp
from app.services.meta_graph import MetaApiError, MetaAuthError, MetaTransientError
from app.services.meta_signature import hash_verify_token

router = APIRouter(prefix="/meta", tags=["meta"])

log = logging.getLogger("app.meta.admin")

ReadCtx = Annotated[TenantContext, Depends(require_minimum_role(Role.MEMBER))]
AdminCtx = Annotated[TenantContext, Depends(require_minimum_role(Role.ADMIN))]
SettingsDep = Annotated[Settings, Depends(get_settings)]

# Fields the API is allowed to write. The credential columns are absent by construction:
# a token only ever arrives through the OAuth callback, never through this endpoint.
#
# `instagram_business_id` is absent for Phase 10-O.2 and that is deliberate. It used to be
# writable here, which meant any tenant admin could attach *any* Instagram id to their Page
# without Meta ever confirming the two are related - and `object: "instagram"` webhooks
# route by exactly that column, so it is an inbound-routing input, not a display setting.
# It now only changes through `POST /connections/{id}/instagram`, which re-queries Meta
# first.
#
# `whatsapp_phone_number_id` is absent for the same reason in Phase 10-O.3, and with more
# force: it is the address outbound messages are POSTed to, so accepting it from a request
# body would let an admin aim this deployment's automated replies at a number that is not
# theirs. It changes only through `POST /connections/{id}/whatsapp`.
#
# `whatsapp_business_account_id` is absent for the same reason. It used to be writable here,
# which made it a second, unverified way to set a trusted provider id: an admin could bind
# any WABA, and `object: "whatsapp_business_account"` webhooks route by exactly that column.
# Phase 10-O.3 closes it - it now only changes through
# `POST /connections/{id}/whatsapp/business-account`, which asks Meta to confirm the account
# for this connection's token first.
_WRITABLE = {
    "display_name",
    "page_id",
    "app_id",
    "status",
    "webhook_verify_token_hash",
}


def _to_response(data: dict) -> MetaConnectionResponse:
    return MetaConnectionResponse(**data)


async def _optional_principal(request: Request, session: DbSession) -> Principal | None:
    """Best-effort session lookup, for the OAuth callback only.

    Deliberately not `get_current_principal` as a hard dependency: an absent or invalid
    session must not turn a legitimate callback into a 401, because the callback is
    public by design - Meta redirects the browser and the session may be gone. When a
    session *does* resolve, its user id becomes an extra binding to check.
    """
    if not request.cookies.get(SESSION_COOKIE_NAME):
        return None
    try:
        return await get_current_principal(request, session)
    except ApiError:
        return None


@router.post(
    "/connections",
    response_model=MetaConnectionResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a Meta connection for the current tenant.",
    responses=error_responses(401, 403, 409),
)
async def create_connection(
    payload: MetaConnectionCreate,
    session: CurrentUserSession,
    context: AdminCtx,
) -> MetaConnectionResponse:
    values = _write_values(payload.model_dump())
    try:
        connection = await connection_repo.create_connection(
            session, tenant_id=context.tenant_id, values=values
        )
    except IntegrityError:
        # A duplicate active page_id / instagram id, or a duplicate slug-like value.
        log.warning(
            "meta_connection_conflict",
            extra={"page_id": payload.page_id, "conflict": True},
        )
        raise ApiError(
            409,
            ErrorCode.META_IDENTIFIER_CONFLICT,
            "That Meta identifier is already used by an active connection.",
        ) from None

    log.info(
        "meta_connection_created",
        extra={"meta_connection_id": str(connection.id), "status": connection.status.value},
    )
    return _to_response(connection_repo.redact(connection))


@router.get(
    "/connections",
    response_model=list[MetaConnectionResponse],
    summary="List the current tenant's Meta connections.",
    responses=error_responses(401, 403),
)
async def list_connections(
    session: CurrentUserSession, _context: ReadCtx
) -> list[MetaConnectionResponse]:
    connections = await connection_repo.list_connections(session)
    return [_to_response(connection_repo.redact(c)) for c in connections]


@router.get(
    "/connections/{connection_id}",
    response_model=MetaConnectionResponse,
    summary="Fetch one Meta connection.",
    responses=error_responses(401, 403, 404),
)
async def get_connection(
    connection_id: uuid.UUID,
    session: CurrentUserSession,
    _context: ReadCtx,
) -> MetaConnectionResponse:
    connection = await connection_repo.get_connection(session, connection_id)
    if connection is None:
        # RLS already hid another tenant's row; 404 keeps it indistinguishable.
        raise ApiError(404, ErrorCode.META_CONNECTION_NOT_FOUND, "Connection not found.")
    return _to_response(connection_repo.redact(connection))


@router.patch(
    "/connections/{connection_id}",
    response_model=MetaConnectionResponse,
    summary="Update a Meta connection.",
    responses=error_responses(401, 403, 404, 409),
)
async def update_connection(
    connection_id: uuid.UUID,
    payload: MetaConnectionUpdate,
    session: CurrentUserSession,
    context: AdminCtx,
) -> MetaConnectionResponse:
    connection = await connection_repo.get_connection(session, connection_id)
    if connection is None:
        raise ApiError(404, ErrorCode.META_CONNECTION_NOT_FOUND, "Connection not found.")

    try:
        await connection_repo.update_connection(
            session, connection, _write_values(payload.changed_fields())
        )
    except IntegrityError:
        raise ApiError(
            409,
            ErrorCode.META_IDENTIFIER_CONFLICT,
            "That Meta identifier is already used by an active connection.",
        ) from None

    log.info(
        "meta_connection_updated",
        extra={"meta_connection_id": str(connection.id), "status": connection.status.value},
    )
    return _to_response(connection_repo.redact(connection))


@router.get(
    "/connections/{connection_id}/events",
    response_model=list[WebhookEventResponse],
    summary="Recent webhook events received for this connection (metadata only).",
    responses=error_responses(401, 403, 404),
)
async def list_connection_events(
    connection_id: uuid.UUID,
    session: CurrentUserSession,
    _context: ReadCtx,
) -> list[WebhookEventResponse]:
    connection = await connection_repo.get_connection(session, connection_id)
    if connection is None:
        raise ApiError(404, ErrorCode.META_CONNECTION_NOT_FOUND, "Connection not found.")

    # Scoped by RLS as well as by the connection, so this cannot read across tenants.
    result = await session.execute(
        select(MetaWebhookEvent)
        .where(MetaWebhookEvent.meta_connection_id == connection_id)
        .order_by(MetaWebhookEvent.received_at.desc())
        .limit(50)
    )
    return [
        WebhookEventResponse(
            id=event.id,
            object_type=event.object_type,
            event_type=event.event_type,
            meta_event_id=event.meta_event_id,
            status=event.status.value,
            received_at=event.received_at,
            processed_at=event.processed_at,
            error_message=event.error_message,
        )
        for event in result.scalars().all()
    ]


# ------------------------------------------------------------------ OAuth


@router.get(
    "/oauth/start",
    response_model=OAuthStartResponse,
    summary="Begin the Meta OAuth flow. Returns the Meta dialog URL to visit.",
    responses=error_responses(401, 403, 503),
)
async def start_oauth(
    session: CurrentUserSession,
    context: AdminCtx,
    settings: SettingsDep,
) -> OAuthStartResponse:
    """Create a single-use state row bound to the authenticated tenant and user.

    `X-Tenant-ID` still only *selects* among the caller's real memberships: the
    membership is verified before this runs, and the tenant that ends up on the
    connection is the one recorded in the state row, not one read back from the URL.
    """
    try:
        _raw_state, url = await meta_oauth.start_oauth(
            session,
            settings=settings,
            tenant_id=context.tenant_id,
            user_id=context.user_id,
        )
    except meta_oauth.OAuthNotConfigured:
        raise ApiError(
            503,
            ErrorCode.META_OAUTH_NOT_CONFIGURED,
            "Meta OAuth is not configured on this deployment.",
        ) from None
    except meta_oauth.OAuthStateRejected:
        raise ApiError(
            400, ErrorCode.META_OAUTH_STATE_INVALID, "Could not start the Meta connection."
        ) from None

    return OAuthStartResponse(authorize_url=url, expires_in=settings.meta_oauth_state_ttl_seconds)


@router.get(
    "/oauth/callback",
    response_model=OAuthCallbackResponse,
    summary="Meta's OAuth redirect target. Anonymous; authorized by single-use state.",
    responses={
        400: {"description": "Missing, expired, reused or foreign OAuth state."},
        401: {"description": "The signed-in user is not the one who started the flow."},
        409: {"description": "The authorized page belongs to another active connection."},
        502: {"description": "Meta rejected the code exchange."},
        503: {"description": "Meta OAuth is unconfigured or temporarily unavailable."},
    },
)
async def oauth_callback(
    request: Request,
    session: DbSession,
    settings: SettingsDep,
    principal: Annotated[Principal | None, Depends(_optional_principal)] = None,
) -> OAuthCallbackResponse:
    """Complete the flow.

    No session is required: Meta redirects the browser and the session may well have
    been lost. When a session *does* exist it is used as an extra check that the person
    finishing the flow is the one who started it. The tenant always comes from the state
    row - never from a parameter on this URL.
    """
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        raise ApiError(
            400,
            ErrorCode.META_OAUTH_STATE_INVALID,
            "The Meta connection request is no longer valid.",
        )

    try:
        connection = await meta_oauth.complete_oauth(
            session,
            # A separate short transaction so the state stays consumed even when this
            # request later fails and rolls back.
            state_factory=get_session_factory(),
            settings=settings,
            code=code,
            state=state,
            expected_user_id=principal.user_id if principal else None,
        )
    except meta_oauth.OAuthNotConfigured:
        raise ApiError(
            503,
            ErrorCode.META_OAUTH_NOT_CONFIGURED,
            "Meta OAuth is not configured on this deployment.",
        ) from None
    except meta_oauth.OAuthStateRejected:
        raise ApiError(
            400,
            ErrorCode.META_OAUTH_STATE_INVALID,
            "The Meta connection request is no longer valid.",
        ) from None
    except meta_oauth.NoAuthorizedPage:
        raise ApiError(
            400,
            ErrorCode.META_OAUTH_NO_PAGE,
            "No Facebook Page with lead access was shared. Grant access to a Page that "
            "runs lead ads, then try again.",
        ) from None
    except MetaAuthError:
        # Meta said the code or the app secret was wrong. Nothing the caller can fix by
        # retrying, and the detail is Meta's, not ours.
        raise ApiError(
            502,
            ErrorCode.META_TOKEN_EXCHANGE_FAILED,
            "Meta rejected the authorization. Start the connection again.",
        ) from None
    except MetaTransientError:
        raise ApiError(
            503,
            ErrorCode.META_API_UNAVAILABLE,
            "Meta is temporarily unavailable. Start the connection again in a moment.",
        ) from None
    except MetaApiError:
        raise ApiError(
            502,
            ErrorCode.META_TOKEN_EXCHANGE_FAILED,
            "Meta rejected the authorization. Start the connection again.",
        ) from None
    except IntegrityError:
        raise ApiError(
            409,
            ErrorCode.META_IDENTIFIER_CONFLICT,
            "That Page is already connected to an active connection.",
        ) from None

    return OAuthCallbackResponse(
        connection=MetaConnectionResponse(**connection_repo.redact(connection)),
        granted_page_count=1,
    )


@router.delete(
    "/connections/{connection_id}",
    response_model=MetaConnectionResponse,
    summary="Disconnect a Meta connection. Leads are kept.",
    responses=error_responses(401, 403, 404),
)
async def disconnect_connection(
    connection_id: uuid.UUID,
    session: CurrentUserSession,
    context: AdminCtx,
) -> MetaConnectionResponse:
    """Deactivate and clear the credential.

    Not a delete: the tenant's leads survive, and the row remains so the history of
    where a lead came from is not lost. `resolve_meta_connection` only matches ACTIVE
    connections, so once this returns, new webhooks for that page stop resolving and
    no further leads are ingested.
    """
    connection = await connection_repo.get_connection(session, connection_id)
    if connection is None:
        raise ApiError(404, ErrorCode.META_CONNECTION_NOT_FOUND, "Connection not found.")

    await meta_oauth.disconnect(session, connection)
    return MetaConnectionResponse(**connection_repo.redact(connection))


# ------------------------------------------------- Instagram Business Accounts


@router.get(
    "/connections/{connection_id}/instagram-accounts",
    response_model=InstagramAccountsResponse,
    summary="Instagram Business Accounts this connection's Page can operate.",
    responses=error_responses(401, 403, 404, 422, 502, 503),
)
async def list_instagram_accounts(
    connection_id: uuid.UUID,
    session: CurrentUserSession,
    _context: AdminCtx,
    settings: SettingsDep,
) -> InstagramAccountsResponse:
    """Discover, from the stored credential, what the tenant may choose from.

    An empty `accounts` list is a 200, not a 404: "this Page has no Instagram Business
    Account" is the answer to the question, and the settings screen needs to say so rather
    than render a failure.

    ADMIN rather than MEMBER because this call spends the tenant's Meta credential on a live
    Graph request. Reading the connection itself stays MEMBER.
    """
    connection = await connection_repo.get_connection(session, connection_id)
    if connection is None:
        raise ApiError(404, ErrorCode.META_CONNECTION_NOT_FOUND, "Connection not found.")

    try:
        options = await meta_instagram.discover_accounts(connection=connection, settings=settings)
    except meta_instagram.InstagramNotSelectable as exc:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, str(exc)) from None
    except MetaAuthError:
        raise ApiError(
            502,
            ErrorCode.META_TOKEN_EXCHANGE_FAILED,
            "Meta rejected this connection's authorization. Reconnect Meta to choose an "
            "Instagram account.",
        ) from None
    except MetaTransientError:
        raise ApiError(
            503,
            ErrorCode.META_API_UNAVAILABLE,
            "Meta is temporarily unavailable. Try again in a moment.",
        ) from None
    except MetaApiError:
        raise ApiError(
            502,
            ErrorCode.META_TOKEN_EXCHANGE_FAILED,
            "Meta could not report this Page's Instagram accounts. Try again.",
        ) from None

    return InstagramAccountsResponse(
        page_id=connection.page_id or "",
        page_name=connection.display_name,
        connected_id=connection.instagram_business_id,
        accounts=[
            InstagramAccountOptionResponse(
                instagram_business_id=option.instagram_business_id,
                username=option.username,
                name=option.name,
                label=option.label,
            )
            for option in options
        ],
    )


@router.post(
    "/connections/{connection_id}/instagram",
    response_model=MetaConnectionResponse,
    summary="Attach an Instagram Business Account to this connection.",
    responses=error_responses(401, 403, 404, 409, 422, 502, 503),
)
async def select_instagram_account(
    connection_id: uuid.UUID,
    payload: InstagramSelectRequest,
    session: CurrentUserSession,
    _context: AdminCtx,
    settings: SettingsDep,
) -> MetaConnectionResponse:
    """Persist the chosen Instagram Business Account, after Meta confirms the relationship.

    The submitted id is treated as a proposal. `meta_instagram.select_account` re-queries
    Meta for this Page's accounts and stores only a match, so an id the server never
    offered - or one that belongs to a different Page, or to a connection another tenant
    holds - cannot be written. The connection is resolved through RLS first, so a cross
    -tenant connection id is a 404 before any of that.
    """
    connection = await connection_repo.get_connection(session, connection_id)
    if connection is None:
        raise ApiError(404, ErrorCode.META_CONNECTION_NOT_FOUND, "Connection not found.")

    try:
        connection = await meta_instagram.select_account(
            session,
            connection=connection,
            instagram_business_id=payload.instagram_business_id,
            settings=settings,
        )
    except meta_instagram.InstagramAccountNotFound as exc:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, str(exc)) from None
    except meta_instagram.InstagramNotSelectable as exc:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, str(exc)) from None
    except IntegrityError:
        # The partial unique index on ACTIVE instagram_business_id: another tenant
        # already operates this account, and Meta will not tell us it is shared.
        raise ApiError(
            409,
            ErrorCode.META_IDENTIFIER_CONFLICT,
            "That Instagram Business Account is already connected to another workspace.",
        ) from None
    except MetaAuthError:
        raise ApiError(
            502,
            ErrorCode.META_TOKEN_EXCHANGE_FAILED,
            "Meta rejected this connection's authorization. Reconnect Meta to choose an "
            "Instagram account.",
        ) from None
    except MetaTransientError:
        raise ApiError(
            503,
            ErrorCode.META_API_UNAVAILABLE,
            "Meta is temporarily unavailable. Try again in a moment.",
        ) from None
    except MetaApiError:
        raise ApiError(
            502,
            ErrorCode.META_TOKEN_EXCHANGE_FAILED,
            "Meta could not confirm this Instagram account. Try again.",
        ) from None

    return MetaConnectionResponse(**connection_repo.redact(connection))


@router.get(
    "/connections/{connection_id}/whatsapp-numbers",
    response_model=WhatsAppNumbersResponse,
    summary="Phone numbers this connection's WhatsApp Business Account can send from.",
    responses=error_responses(401, 403, 404, 422, 502, 503),
)
async def list_whatsapp_numbers(
    connection_id: uuid.UUID,
    session: CurrentUserSession,
    _context: AdminCtx,
    settings: SettingsDep,
) -> WhatsAppNumbersResponse:
    """Discover, from the stored credential, which numbers the tenant may choose.

    An empty `numbers` list is a 200: "this account has no number" is the answer, and the
    screen needs to say so rather than render a failure.
    """
    connection = await connection_repo.get_connection(session, connection_id)
    if connection is None:
        raise ApiError(404, ErrorCode.META_CONNECTION_NOT_FOUND, "Connection not found.")

    try:
        options = await meta_whatsapp.discover_numbers(connection=connection, settings=settings)
    except meta_whatsapp.WhatsAppNotConfigurable as exc:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, str(exc)) from None
    except MetaAuthError:
        raise ApiError(
            502,
            ErrorCode.META_TOKEN_EXCHANGE_FAILED,
            "Meta rejected this connection's authorization. Reconnect Meta to choose a "
            "WhatsApp number.",
        ) from None
    except MetaTransientError:
        raise ApiError(
            503,
            ErrorCode.META_API_UNAVAILABLE,
            "Meta is temporarily unavailable. Try again in a moment.",
        ) from None
    except MetaApiError:
        raise ApiError(
            502,
            ErrorCode.META_TOKEN_EXCHANGE_FAILED,
            "Meta could not report this account's phone numbers. Try again.",
        ) from None

    return WhatsAppNumbersResponse(
        whatsapp_business_account_id=connection.whatsapp_business_account_id,
        connected_id=connection.whatsapp_phone_number_id,
        numbers=[
            WhatsAppNumberOptionResponse(
                phone_number_id=option.phone_number_id,
                display_number=option.display_number,
                verified_name=option.verified_name,
                is_verified=option.is_verified,
                label=option.label,
            )
            for option in options
        ],
    )


@router.post(
    "/connections/{connection_id}/whatsapp",
    response_model=MetaConnectionResponse,
    summary="Choose the WhatsApp number this connection sends from.",
    responses=error_responses(401, 403, 404, 409, 422, 502, 503),
)
async def select_whatsapp_number(
    connection_id: uuid.UUID,
    payload: WhatsAppNumberSelectRequest,
    session: CurrentUserSession,
    _context: AdminCtx,
    settings: SettingsDep,
) -> MetaConnectionResponse:
    """Persist the chosen number, after Meta confirms the account owns it.

    The submitted id is a proposal: `meta_whatsapp.select_number` re-queries Meta and stores
    only a match. The connection is resolved through RLS first, so a cross-tenant id is a
    404 before any credential is touched.
    """
    connection = await connection_repo.get_connection(session, connection_id)
    if connection is None:
        raise ApiError(404, ErrorCode.META_CONNECTION_NOT_FOUND, "Connection not found.")

    try:
        connection = await meta_whatsapp.select_number(
            session,
            connection=connection,
            phone_number_id=payload.phone_number_id,
            settings=settings,
        )
    except meta_whatsapp.WhatsAppNumberNotFound as exc:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, str(exc)) from None
    except meta_whatsapp.WhatsAppNotConfigurable as exc:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, str(exc)) from None
    except IntegrityError:
        raise ApiError(
            409,
            ErrorCode.META_IDENTIFIER_CONFLICT,
            "That WhatsApp number is already connected to another workspace.",
        ) from None
    except MetaAuthError:
        raise ApiError(
            502,
            ErrorCode.META_TOKEN_EXCHANGE_FAILED,
            "Meta rejected this connection's authorization. Reconnect Meta to choose a "
            "WhatsApp number.",
        ) from None
    except MetaTransientError:
        raise ApiError(
            503,
            ErrorCode.META_API_UNAVAILABLE,
            "Meta is temporarily unavailable. Try again in a moment.",
        ) from None
    except MetaApiError:
        raise ApiError(
            502,
            ErrorCode.META_TOKEN_EXCHANGE_FAILED,
            "Meta could not confirm this phone number. Try again.",
        ) from None

    return MetaConnectionResponse(**connection_repo.redact(connection))


@router.post(
    "/connections/{connection_id}/whatsapp/business-account",
    response_model=MetaConnectionResponse,
    summary="Bind the WhatsApp Business Account this connection sends from.",
    responses=error_responses(401, 403, 404, 422, 502, 503),
)
async def select_whatsapp_business_account(
    connection_id: uuid.UUID,
    payload: WhatsAppAccountSelectRequest,
    session: CurrentUserSession,
    _context: AdminCtx,
    settings: SettingsDep,
) -> MetaConnectionResponse:
    """Persist a WhatsApp Business Account, after Meta confirms it for this connection.

    The account id is what `object: "whatsapp_business_account"` webhooks are routed by, so
    it is a trusted identifier and never taken from a request body on trust. Meta is asked
    first, with this connection's own token: an id Meta will not answer for is refused, which
    is what stops a tenant binding an account they do not control.

    The connection is resolved through RLS before any credential is touched, so a
    cross-tenant id is a 404 rather than an oracle.
    """
    connection = await connection_repo.get_connection(session, connection_id)
    if connection is None:
        raise ApiError(404, ErrorCode.META_CONNECTION_NOT_FOUND, "Connection not found.")

    try:
        connection = await meta_whatsapp.associate_business_account(
            session,
            connection=connection,
            whatsapp_business_account_id=payload.whatsapp_business_account_id,
            settings=settings,
        )
    except meta_whatsapp.WhatsAppAccountNotFound as exc:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, str(exc)) from None
    except meta_whatsapp.WhatsAppNotConfigurable as exc:
        raise ApiError(422, ErrorCode.VALIDATION_ERROR, str(exc)) from None
    except IntegrityError:
        # One WhatsApp Business Account belongs to one workspace: a second tenant binding it
        # would mean inbound webhooks for it resolved to whichever row answered first.
        raise ApiError(
            409,
            ErrorCode.META_IDENTIFIER_CONFLICT,
            "That WhatsApp Business Account is already connected to another workspace.",
        ) from None
    except MetaAuthError:
        raise ApiError(
            502,
            ErrorCode.META_TOKEN_EXCHANGE_FAILED,
            "Meta rejected this connection's authorization. Reconnect Meta to choose a "
            "WhatsApp Business Account.",
        ) from None
    except MetaTransientError:
        raise ApiError(
            503,
            ErrorCode.META_API_UNAVAILABLE,
            "Meta is temporarily unavailable. Try again in a moment.",
        ) from None
    except MetaApiError:
        raise ApiError(
            502,
            ErrorCode.META_TOKEN_EXCHANGE_FAILED,
            "Meta could not confirm this WhatsApp Business Account. Try again.",
        ) from None

    return MetaConnectionResponse(**connection_repo.redact(connection))


def _write_values(values: dict) -> dict:
    """Translate the write-only `webhook_verify_token` into its stored digest.

    The token is popped *before* the allowlist filter: it is a field the caller may
    send, but not a column it may write.
    """
    values = dict(values)
    token = values.pop("webhook_verify_token", None)
    values = {k: v for k, v in values.items() if k in _WRITABLE and v is not None}
    if token is not None:
        values["webhook_verify_token_hash"] = hash_verify_token(token)
    return values
