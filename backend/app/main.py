"""Application bootstrap.

`create_app` is the single entry point used by uvicorn (``app.main:app``) and by
the test suite, so the whole wiring lives in one factory.
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.health import router as health_router
from app.api.router import api_router
from app.core.config import get_settings
from app.core.errors import register_exception_handlers
from app.core.logging import configure_logging
from app.core.middleware import RequestContextMiddleware
from app.db.session import close_database
from app.services.follow_up_lifecycle import start_scheduler, stop_scheduler

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    logger.info("application_started", extra={"api_prefix": get_settings().api_prefix})
    # The follow-up poller is started here rather than at import time, so it does not fire
    # during tests or Alembic runs. It is a trigger only - `scheduled_tasks` is the durable
    # source of truth - and a failure to start must not take the API down, since inbound
    # messages matter more than the automation.
    scheduler = await start_scheduler()
    try:
        yield
    finally:
        await stop_scheduler(scheduler)
        await close_database()
        logger.info("application_stopped")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        # Never enable debug: it leaks internals in responses.
        debug=False,
        docs_url=f"{settings.api_prefix}/docs",
        openapi_url=f"{settings.api_prefix}/openapi.json",
        lifespan=lifespan,
    )

    # Order matters: the first one added is the outermost.
    app.add_middleware(RequestContextMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=[settings.request_id_header],
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_prefix)
    app.include_router(health_router)

    return app


app = create_app()
