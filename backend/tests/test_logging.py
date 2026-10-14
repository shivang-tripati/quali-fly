import io
import json
import logging

from app.core.config import Settings
from app.core.logging import JsonFormatter, configure_logging, request_id_var


def _emit(**extra) -> str:
    """Log through a real logger and return the formatted JSON line."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter(environment="test"))

    logger = logging.getLogger("app.test")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.INFO)
    try:
        logger.info("hello", extra=extra)
    finally:
        logger.handlers = []
    return stream.getvalue().strip()


def test_formatter_emits_required_fields():
    payload = json.loads(_emit())

    assert payload["level"] == "INFO"
    assert payload["logger"] == "app.test"
    assert payload["message"] == "hello"
    assert payload["environment"] == "test"
    assert payload["timestamp"].endswith("+00:00")


def test_formatter_includes_request_id_from_context():
    token = request_id_var.set("req-42")
    try:
        payload = json.loads(_emit())
    finally:
        request_id_var.reset(token)

    assert payload["request_id"] == "req-42"


def test_formatter_includes_extra_fields():
    payload = json.loads(_emit(http_status=200, duration_ms=1.5))

    assert payload["http_status"] == 200
    assert payload["duration_ms"] == 1.5


def test_configure_logging_is_idempotent():
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        app_env="test",
        database_url="postgresql+asyncpg://u:p@db:5432/app",
    )
    configure_logging(settings)
    configure_logging(settings)

    assert len(logging.getLogger().handlers) == 1
    assert logging.getLogger("uvicorn.access").disabled is True
