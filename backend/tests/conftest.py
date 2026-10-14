"""Shared test fixtures.

Unit tests (health, config, logging, security helpers) need nothing but imports.
Database-backed tests need `TEST_DATABASE_URL`; when it is set the whole suite
points at that database and migrations are applied once per session.

Two roles are involved, exactly as in production:

    owner  - runs migrations, seeds fixture data, truncates between tests
    app    - TEST_DATABASE_URL: the unprivileged role the API actually uses

That split is what makes the RLS tests meaningful: a superuser or the table owner
bypasses every policy, so the app role must be neither.
"""

import os

import pytest


def _test_token_key() -> str:
    """base64url of 32 deterministic bytes, in the shape the app's crypto expects."""
    import base64
    import hashlib

    return base64.urlsafe_b64encode(hashlib.sha256(b"quali-fly-test-token-key").digest()).decode()


# Must be set before any app module reads the environment.
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@127.0.0.1:5432/test")

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")
TEST_MIGRATION_DATABASE_URL = os.getenv("TEST_MIGRATION_DATABASE_URL") or TEST_DATABASE_URL
# Must match the signature the test suite sends; the endpoint fails closed without one.
TEST_META_APP_SECRET = os.getenv("TEST_META_APP_SECRET", "test-meta-app-secret")
# A dedicated deterministic key so token encryption is exercised for real, without ever
# depending on a developer's production key. Derived rather than pasted, because a
# hand-written base64 string is exactly the kind of thing that silently stops decoding
# to 32 bytes.
TEST_META_TOKEN_KEY = os.getenv("TEST_META_TOKEN_KEY") or _test_token_key()
# OAuth is unconfigured unless a test asks for it, so tests that do not care about it are
# not silently coupled to a Meta app existing.
TEST_META_APP_ID = os.getenv("TEST_META_APP_ID") or None
TEST_META_OAUTH_REDIRECT_URI = (
    os.getenv("TEST_META_OAUTH_REDIRECT_URI")
    or "https://app.example.test/api/v1/meta/oauth/callback"
)
if TEST_DATABASE_URL:
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL
    os.environ["MIGRATION_DATABASE_URL"] = TEST_MIGRATION_DATABASE_URL
    os.environ["META_APP_SECRET"] = TEST_META_APP_SECRET
    os.environ["META_TOKEN_ENCRYPTION_KEY"] = TEST_META_TOKEN_KEY
    os.environ.setdefault("APP_DB_ROLE", "quali_fly_app")
    os.environ["APP_ENV"] = "test"

from app.core.config import get_settings  # noqa: E402

# Children before parents, so CASCADE never fights the truncate order.
_TABLES_IN_TRUNCATE_ORDER = (
    "sessions",
    "meta_oauth_states",
    # Ordered children before parents. `ai_message_runs` is here explicitly even though
    # `leads` would cascade to it: a truncation list that depends on cascade chains is one
    # refactor away from silently leaving rows behind.
    "ai_message_runs",
    "knowledge_base",
    "scheduled_tasks",
    # Phase 8. Listed explicitly rather than left to `tenants CASCADE`, for the same
    # reason `ai_message_runs` is: a handoff row that survives a test would make the next
    # test's "one active handoff" assertion pass or fail for the wrong reason.
    "internal_notifications",
    "human_handoffs",
    "leads",
    "meta_webhook_events",
    "meta_connections",
    "tenant_memberships",
    "tenants",
    "users",
)


def _run_migrations(database_url: str) -> None:
    """Apply the real Alembic migrations so tests exercise the real schema + RLS."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    command.upgrade(config, "head")


def _factory_for(database_url: str):
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    engine = create_async_engine(database_url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


@pytest.fixture(scope="session")
def live_database() -> str:
    if not TEST_DATABASE_URL:
        pytest.skip("set TEST_DATABASE_URL to run database-backed tests")
    _run_migrations(TEST_MIGRATION_DATABASE_URL)
    return TEST_DATABASE_URL


async def _truncate_all(factory) -> None:
    async with factory() as session, session.begin():
        for table in _TABLES_IN_TRUNCATE_ORDER:
            await session.execute(_truncate(table))


@pytest.fixture
async def owner(live_database: str):
    """Privileged sessions for fixture data and truncation only."""
    from app.db.session import close_database

    engine, factory = _factory_for(TEST_MIGRATION_DATABASE_URL)
    await _truncate_all(factory)

    try:
        yield factory
    finally:
        # Leave the test database empty: leftover rows would otherwise look like
        # real seed data to the next command that touches this database.
        await _truncate_all(factory)
        await engine.dispose()
        await close_database()


@pytest.fixture
async def db(live_database: str, owner):
    """The application's own sessions: unprivileged, so RLS applies to them."""
    from app.db.session import close_database, get_session_factory

    get_settings.cache_clear()
    try:
        yield get_session_factory()
    finally:
        await close_database()
        get_settings.cache_clear()


def _truncate(table: str):
    from sqlalchemy import text

    return text(f"TRUNCATE TABLE {table} CASCADE")


@pytest.fixture
def app():
    """Application instance. Does not touch the database, so no live DB is needed."""
    from app.main import create_app

    get_settings.cache_clear()
    return create_app()


@pytest.fixture
def client(app):
    """In-process ASGI client. No live database needed for the liveness tests."""
    from fastapi.testclient import TestClient

    with TestClient(app, raise_server_exceptions=False) as test_client:
        yield test_client


@pytest.fixture
async def api(app, db):
    """Async in-process client for database-backed endpoint tests.

    Async on purpose: sharing one event loop with the database fixtures avoids
    "attached to a different loop" errors from the asyncpg pool.
    """
    import httpx

    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest.fixture
def oauth_settings():
    """Settings with Meta OAuth configured, for tests that exercise the flow.

    `model_copy` rather than a re-construction: rebuilding would re-read the environment
    and discard the overrides, and re-running the production validator is not what these
    tests are about.
    """
    from tests.conftest import TEST_META_APP_ID, TEST_META_OAUTH_REDIRECT_URI

    return get_settings().model_copy(
        update={
            "meta_app_id": TEST_META_APP_ID or "1234567890",
            "meta_app_secret": "test-meta-app-secret",
            "meta_oauth_redirect_uri": TEST_META_OAUTH_REDIRECT_URI,
        }
    )


@pytest.fixture
async def oauth_api(app, db, oauth_settings):
    """An API client whose endpoints see OAuth as configured.

    A FastAPI dependency override rather than mutating the environment: the real
    settings genuinely have no Meta app id, and the point of these tests is the flow,
    not the configuration check.
    """
    import httpx

    app.dependency_overrides[get_settings] = lambda: oauth_settings
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
            yield http
    finally:
        app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
def fake_graph():
    """An in-memory `MetaGraphClient` stand-in.

    The database is never mocked in this suite; only the outbound HTTP boundary is,
    which is the one thing we cannot control. `FakeGraph` records what it was asked for
    so a test can assert the Graph call carried the *decrypted* token.
    """
    from tests.fakes import FakeGraph

    return FakeGraph()


# -- Phase 7: follow-up fixtures ---------------------------------------------
# Defined here rather than in a phase-local module so they follow the same rule as every
# other fixture in this file. Putting them in a test module shadows the helper functions
# in `tests/follow_up_fixtures.py`, whose parameters are also called `clock` and
# `conversation` - and pytest test functions have to name their fixtures the same way the
# fixture is named.


@pytest.fixture
def clock():
    """A manually advanced UTC clock.

    Phase 7's correctness is a function of time - a task due at +4h, a Meta window closing
    at 24h, a lease expiring at 300s - so the tests advance this instead of sleeping. A
    suite that waited four real hours would not be run, and the tests that matter most,
    the cancellation race and the ghosted boundary, are exactly the ones that would need it.
    """
    from datetime import UTC, datetime

    from app.core.clock import FixedClock

    return FixedClock(datetime(2026, 9, 28, 10, 0, 0, tzinfo=UTC))


@pytest.fixture
async def tenant_row(owner):
    """An active tenant. `tenants` is the isolation root and is not RLS-scoped."""
    import uuid as _uuid

    from tests.factories import create_tenant

    return await create_tenant(owner, name="Acme", slug=f"acme-{_uuid.uuid4().hex[:8]}")


@pytest.fixture
async def other_tenant_row(owner):
    """A second tenant, so an isolation test has another tenant's row to find."""
    import uuid as _uuid

    from tests.factories import create_tenant

    return await create_tenant(owner, name="Globex", slug=f"globex-{_uuid.uuid4().hex[:8]}")


@pytest.fixture
async def conversation(owner, tenant_row):
    """A page connection, a conversation, one inbound message, and a lead."""
    from tests.follow_up_fixtures import make_conversation

    return await make_conversation(owner, tenant_row)
