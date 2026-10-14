"""Meta webhook endpoints.

Authentication here is Meta's, not the SaaS session:

  * verification: the per-connection verification token, compared inside PostgreSQL
  * delivery:     HMAC-SHA256 over the exact raw request body

Neither path reads a session cookie or `X-Tenant-ID`, and neither can reach tenant
data on its own. The tenant is derived from the Meta source identifier, never from the
request. A caller holding a valid SaaS session but no Meta signature gets nothing.
"""

import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy import text

from app.api.deps import DbSession
from app.core.config import Settings, get_settings
from app.core.errors import ApiError, ErrorCode
from app.schemas.meta import WebhookAck
from app.services.meta_signature import SignatureError, hash_verify_token, verify_signature
from app.services.meta_webhook import MalformedWebhook, ingest

router = APIRouter(prefix="/webhooks/meta", tags=["webhooks"])

log = logging.getLogger("app.meta.webhook")

SettingsDep = Annotated[Settings, Depends(get_settings)]

# The verification token is compared by PostgreSQL, so the stored digest is never
# returned to the application. This is a blind lookup: match or no match.
_VERIFY_TOKEN_SQL = text(
    """
    SELECT connection_id, tenant_id, provider
    FROM verify_meta_webhook_token(:connection_id, :token_hash)
    """
)


def _reject(message: str = "Webhook rejected.") -> ApiError:
    """One failure mode, one code. Splitting them would leak which guess was close."""
    return ApiError(403, ErrorCode.META_WEBHOOK_SIGNATURE_INVALID, message)


@router.get(
    "/{connection_id}",
    response_class=PlainTextResponse,
    summary="Meta webhook verification for one connection.",
    responses={403: {"description": "Unknown connection or invalid verification token."}},
)
async def verify_webhook(
    connection_id: str,
    request: Request,
    session: DbSession,
) -> PlainTextResponse:
    """Answer Meta's `hub.mode=subscribe` challenge.

    The connection is addressed by id, so every tenant verifies with its own token:
    one tenant's token never validates another tenant's connection.
    """
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode != "subscribe" or not challenge or not token:
        log.warning("webhook_rejected", extra={"reason": "invalid_verification_request"})
        raise _reject()

    try:
        parsed_id = uuid.UUID(connection_id)
    except ValueError:
        log.warning("webhook_rejected", extra={"reason": "malformed_connection_id"})
        raise _reject() from None

    matched = (
        await session.execute(
            _VERIFY_TOKEN_SQL,
            {"connection_id": parsed_id, "token_hash": hash_verify_token(token)},
        )
    ).one_or_none()
    if matched is None:
        log.warning("webhook_rejected", extra={"reason": "invalid_verification_token"})
        raise _reject()

    log.info("webhook_verified", extra={"meta_connection_id": str(matched.connection_id)})
    return PlainTextResponse(content=challenge)


@router.post(
    "",
    response_model=WebhookAck,
    status_code=200,
    summary="Meta webhook delivery. Authenticated by HMAC-SHA256 over the raw body.",
    responses={
        400: {"description": "Body is not a usable Meta webhook envelope."},
        403: {"description": "Invalid signature."},
        404: {"description": "No active Meta connection owns the source identifier."},
    },
)
async def receive_webhook(
    request: Request,
    session: DbSession,
    settings: SettingsDep,
) -> WebhookAck:
    """Validate, identify, deduplicate, persist, ACK.

    The raw body is read and signed *before* any JSON parsing: re-serialising the
    payload would change the bytes and break verification.
    """
    raw_body = await request.body()
    try:
        verify_signature(
            settings.meta_app_secret,
            raw_body,
            request.headers.get(settings.meta_signature_header),
        )
    except SignatureError as exc:
        log.warning("webhook_rejected", extra={"reason": str(exc)})
        raise _reject() from None

    try:
        result = await ingest(session, raw_body=raw_body, request_headers=request.headers)
    except MalformedWebhook as exc:
        log.warning("webhook_rejected", extra={"reason": "malformed_payload"})
        raise ApiError(400, ErrorCode.META_WEBHOOK_PAYLOAD_INVALID, "Malformed payload.") from exc
    except LookupError:
        # Unknown or INACTIVE connection. 404 rather than 403 so Meta stops retrying
        # a source we have no configuration for. No tenant data leaks.
        raise ApiError(404, ErrorCode.META_CONNECTION_NOT_FOUND, "Unknown Meta source.") from None

    return WebhookAck(status="ok", event_id=result.event_id, duplicate=result.duplicate)
