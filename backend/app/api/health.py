"""Liveness and readiness endpoints.

These live at the server root (not under `API_PREFIX`) so that container health
checks and the Nginx `/health` route stay stable regardless of API versioning.
"""

from fastapi import APIRouter, Response, status

from app.db.session import check_database
from app.schemas.health import HealthResponse, ReadinessResponse

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    """Liveness: the process is running. No dependencies, no secrets."""
    return HealthResponse(status="ok")


@router.get("/health/ready", response_model=ReadinessResponse)
async def readiness(response: Response) -> ReadinessResponse:
    """Readiness: the process can serve traffic, i.e. PostgreSQL answers."""
    try:
        await check_database()
    except Exception:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return ReadinessResponse(status="degraded", checks={"database": "unavailable"})
    return ReadinessResponse(status="ok", checks={"database": "ok"})
