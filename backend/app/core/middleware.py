"""Request ID propagation and access logging."""

import logging
import time
import uuid

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings
from app.core.logging import bind_identity, request_id_var

logger = logging.getLogger("app.request")

_MAX_REQUEST_ID_LENGTH = 128


def _normalise_incoming(value: str | None) -> str | None:
    """Accept a client-supplied request ID only if it is short and printable."""
    if not value:
        return None
    value = value.strip()
    if not value or len(value) > _MAX_REQUEST_ID_LENGTH:
        return None
    if not value.isprintable():
        return None
    return value


def _duration(started_at: float) -> dict[str, float]:
    return {"duration_ms": round((time.perf_counter() - started_at) * 1000, 2)}


class RequestContextMiddleware(BaseHTTPMiddleware):
    """Assigns a request ID, exposes it to logs, echoes it, logs the access line."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        settings = get_settings()
        request_id = _normalise_incoming(request.headers.get(settings.request_id_header))
        if request_id is None:
            request_id = str(uuid.uuid4())

        # read by the catch-all error handler, which runs outside this middleware.
        request.state.request_id = request_id
        started_at = time.perf_counter()
        access = {
            "http_method": request.method,
            "http_path": request.url.path,
        }

        token = request_id_var.set(request_id)
        try:
            response = await call_next(request)
            response.headers[settings.request_id_header] = request_id
            with bind_identity(
                user_id=getattr(request.state, "user_id", None),
                tenant_id=getattr(request.state, "tenant_id", None),
            ):
                logger.info(
                    "request",
                    extra={**access, "http_status": response.status_code, **_duration(started_at)},
                )
            return response
        except Exception:
            logger.exception("request_failed", extra=access)
            raise
        finally:
            request_id_var.reset(token)
