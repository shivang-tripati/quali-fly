import json
import logging
import uuid

from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.core.logging import JsonFormatter
from app.main import create_app


class _CapturingHandler(logging.Handler):
    """Formats on emit so contextvar-based fields are still visible."""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(JsonFormatter(environment="test"))
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(self.format(record))


def test_health_returns_ok(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_is_not_versioned(app, client):
    # Liveness lives at the root so it survives API_PREFIX changes.
    assert client.get("/api/v1/health").status_code == 404


def test_echoes_incoming_request_id(client):
    given = "client-request-id-123"

    response = client.get("/health", headers={"X-Request-ID": given})

    assert response.headers["X-Request-ID"] == given


def test_generates_request_id_when_absent(client):
    response = client.get("/health")

    assert uuid.UUID(response.headers["X-Request-ID"])


def test_ignores_unusable_request_id(client):
    response = client.get("/health", headers={"X-Request-ID": "x" * 500})

    assert uuid.UUID(response.headers["X-Request-ID"])


def test_error_envelope_hides_internals(client):
    response = client.get("/does-not-exist")

    body = response.json()
    assert response.status_code == 404
    assert set(body) == {"error"}
    assert body["error"]["code"] == "not_found"
    assert "request_id" in body["error"]
    assert "Traceback" not in response.text


def test_validation_error_envelope():
    router = APIRouter()

    @router.get("/_probe")
    async def _probe(count: int) -> dict[str, int]:
        return {"count": count}

    probe_app = create_app()
    probe_app.include_router(router)
    with TestClient(probe_app, raise_server_exceptions=False) as probe_client:
        response = probe_client.get("/_probe?count=abc")

    body = response.json()
    assert response.status_code == 422
    assert body["error"]["code"] == "validation_error"
    assert body["error"]["details"][0]["loc"] == ["query", "count"]


def test_readiness_reports_database_state(client, monkeypatch):
    from app.api import health

    async def unavailable() -> None:
        raise RuntimeError("database is down")

    monkeypatch.setattr(health, "check_database", unavailable)
    degraded = client.get("/health/ready")
    assert degraded.status_code == 503
    assert degraded.json() == {"status": "degraded", "checks": {"database": "unavailable"}}
    assert "RuntimeError" not in degraded.text

    async def available() -> None:
        return None

    monkeypatch.setattr(health, "check_database", available)
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json() == {"status": "ok", "checks": {"database": "ok"}}


def test_access_log_carries_the_request_id(client):
    # Formatted during emit(), so it also proves the contextvar is still set.
    handler = _CapturingHandler()
    access_logger = logging.getLogger("app.request")
    access_logger.addHandler(handler)
    try:
        client.get("/health", headers={"X-Request-ID": "req-log-1"})
    finally:
        access_logger.removeHandler(handler)

    line = json.loads(handler.lines[-1])
    assert line["request_id"] == "req-log-1"
    assert line["http_status"] == 200
    assert line["http_path"] == "/health"
    assert line["logger"] == "app.request"


def test_openapi_is_served_under_api_prefix(client):
    response = client.get(f"{get_settings().api_prefix}/openapi.json")

    assert response.status_code == 200
    assert response.json()["info"]["version"] == get_settings().app_version
