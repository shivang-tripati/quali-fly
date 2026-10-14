"""Structured JSON logging plus the request-scoped context that feeds it.

Later phases only need to add `tenant_id` / `user_id` / `conversation_id` as extra
contextvars plus matching keys in `_CONTEXT_KEYS`.
"""

import contextvars
import json
import logging
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from app.core.config import Settings

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id", default=None
)
user_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("user_id", default=None)
tenant_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tenant_id", default=None
)

# Contextvars automatically added to every log record.
_CONTEXT_SOURCES: dict[str, contextvars.ContextVar[str | None]] = {
    "request_id": request_id_var,
    "user_id": user_id_var,
    "tenant_id": tenant_id_var,
}

_STANDARD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}

logger = logging.getLogger("app")


class JsonFormatter(logging.Formatter):
    """One JSON object per line, safe to ship to any log aggregator."""

    def __init__(self, environment: str) -> None:
        super().__init__()
        self.environment = environment

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "environment": self.environment,
        }
        for key, source in _CONTEXT_SOURCES.items():
            value = source.get()
            if value:
                payload[key] = value
        # `extra={...}` fields, minus LogRecord internals.
        payload.update(
            {
                k: v
                for k, v in record.__dict__.items()
                if k not in _STANDARD_ATTRS and k not in payload
            }
        )
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(payload, default=str)


@contextmanager
def bind_identity(*, user_id: str | None = None, tenant_id: str | None = None) -> Iterator[None]:
    """Attach user/tenant to every log line emitted inside the block.

    Called by the auth dependencies so the access log already carries them.
    """
    user_token = user_id_var.set(user_id) if user_id else None
    tenant_token = tenant_id_var.set(tenant_id) if tenant_id else None
    try:
        yield
    finally:
        if tenant_token is not None:
            tenant_id_var.reset(tenant_token)
        if user_token is not None:
            user_id_var.reset(user_token)


def configure_logging(settings: Settings) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(settings.app_env))

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(settings.log_level.upper())

    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = [handler]
        # Access logging is done by RequestContextMiddleware, which adds request_id.
        uvicorn_logger.propagate = False
    logging.getLogger("uvicorn.access").disabled = True

    # httpx writes the full request URL at DEBUG, and the Graph API takes the access
    # token as a query parameter - so a single `LOG_LEVEL=DEBUG` would otherwise push
    # every tenant's Meta token into the log pipeline. The token is required in the URL
    # by Meta's API design, so clamping the loggers is the fix rather than the token.
    for name in ("httpx", "httpcore", "hpack", "h2"):
        logging.getLogger(name).setLevel(logging.WARNING)
