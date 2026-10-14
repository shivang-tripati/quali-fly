"""Consistent, information-safe API error responses.

Every error uses the same envelope:

    {"error": {"code": "...", "message": "...", "details": ..., "request_id": "..."}}

Never expose stack traces, file paths, SQL, or configuration values.
"""

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import get_settings

logger = logging.getLogger("app.error")


class ErrorCode:
    """Stable error codes. Clients may branch on these; messages may change."""

    NOT_FOUND = "not_found"
    HTTP_ERROR = "http_error"
    VALIDATION_ERROR = "validation_error"
    INTERNAL_ERROR = "internal_error"

    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_INVALID = "AUTH_INVALID"
    USER_INACTIVE = "USER_INACTIVE"
    EMAIL_TAKEN = "EMAIL_TAKEN"
    TENANT_REQUIRED = "TENANT_REQUIRED"
    TENANT_NOT_FOUND = "TENANT_NOT_FOUND"
    TENANT_ACCESS_DENIED = "TENANT_ACCESS_DENIED"
    TENANT_INACTIVE = "TENANT_INACTIVE"
    ROLE_REQUIRED = "ROLE_REQUIRED"

    # -- Meta integration ------------------------------------------------
    # One shared code for every signature failure, so an attacker cannot learn
    # whether the secret, the digest, the header or the source was the problem.
    META_WEBHOOK_SIGNATURE_INVALID = "META_WEBHOOK_SIGNATURE_INVALID"
    META_WEBHOOK_PAYLOAD_INVALID = "META_WEBHOOK_PAYLOAD_INVALID"
    META_CONNECTION_NOT_FOUND = "META_CONNECTION_NOT_FOUND"
    META_IDENTIFIER_CONFLICT = "META_IDENTIFIER_CONFLICT"

    # -- Meta OAuth ------------------------------------------------------
    # One code for every state failure: unknown, expired, reused, or a different user.
    # Which one it was is not the caller's business, and saying so would let an attacker
    # probe whether a guessed state had ever existed.
    META_OAUTH_STATE_INVALID = "META_OAUTH_STATE_INVALID"
    META_OAUTH_NOT_CONFIGURED = "META_OAUTH_NOT_CONFIGURED"
    META_OAUTH_DENIED = "META_OAUTH_DENIED"
    META_OAUTH_NO_PAGE = "META_OAUTH_NO_PAGE"
    META_TOKEN_EXCHANGE_FAILED = "META_TOKEN_EXCHANGE_FAILED"
    META_API_UNAVAILABLE = "META_API_UNAVAILABLE"
    META_CREDENTIAL_REJECTED = "META_CREDENTIAL_REJECTED"

    # -- Conversations ----------------------------------------------------
    MESSAGE_NOT_SENDABLE = "MESSAGE_NOT_SENDABLE"

    # -- Human handoff (Phase 8) ------------------------------------------
    # Split by cause rather than collapsed into one code, because the caller's next move
    # differs in each: a missing handoff is a stale page, a claim conflict is "someone got
    # there first, refresh", and an invalid state is "this button no longer applies".
    HANDOFF_NOT_FOUND = "HANDOFF_NOT_FOUND"
    HANDOFF_ALREADY_ASSIGNED = "HANDOFF_ALREADY_ASSIGNED"
    HANDOFF_INVALID_STATE = "HANDOFF_INVALID_STATE"
    HANDOFF_ASSIGNMENT_NOT_ALLOWED = "HANDOFF_ASSIGNMENT_NOT_ALLOWED"
    HANDOFF_RESOLUTION_NOT_ALLOWED = "HANDOFF_RESOLUTION_NOT_ALLOWED"


class ApiError(Exception):
    """Raise this from anywhere; the handler below renders the standard envelope."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


def _envelope(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    details: Any = None,
) -> JSONResponse:
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    request_id = getattr(request.state, "request_id", None)
    if request_id:
        error["request_id"] = request_id
    response = JSONResponse(status_code=status_code, content={"error": error})
    if request_id:
        # The catch-all handler runs outside RequestContextMiddleware, so it must
        # set the header itself.
        response.headers[get_settings().request_id_header] = request_id
    return response


def error_responses(*status_codes: int) -> dict[int | str, dict[str, Any]]:
    """Reusable OpenAPI `responses` entries so the docs describe real failures."""
    descriptions = {
        401: "Authentication required or credentials invalid.",
        403: "Authenticated but not permitted.",
        404: "Not found.",
    }
    return {
        status: {
            "description": descriptions.get(status, "Error."),
            "content": {"application/json": {"example": {"error": {"code": "…", "message": "…"}}}},
        }
        for status in status_codes
    }


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError) -> JSONResponse:
        response = _envelope(
            request,
            status_code=exc.status_code,
            code=exc.code,
            message=exc.message,
        )
        for key, value in exc.headers.items():
            response.headers[key] = value
        return response

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        return _envelope(
            request,
            status_code=exc.status_code,
            code=ErrorCode.NOT_FOUND if exc.status_code == 404 else ErrorCode.HTTP_ERROR,
            message=str(exc.detail),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _envelope(
            request,
            status_code=422,
            code=ErrorCode.VALIDATION_ERROR,
            message="Request validation failed.",
            details=[
                {"loc": [str(part) for part in item["loc"]], "msg": item["msg"]}
                for item in exc.errors()
            ],
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "unhandled_exception",
            extra={"http_method": request.method, "http_path": request.url.path},
        )
        return _envelope(
            request,
            status_code=500,
            code=ErrorCode.INTERNAL_ERROR,
            message="Internal server error. Quote the request_id when reporting this issue.",
        )
