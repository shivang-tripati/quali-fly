import inspect

import pytest
from sqlalchemy import make_url, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.session import check_database, close_database, get_db, get_engine

# A connection that is refused surfaces as OSError on some platforms and as a
# SQLAlchemyError on others; both mean "database not reachable".
UNREACHABLE = (SQLAlchemyError, OSError)


def test_get_db_is_an_async_generator_dependency():
    assert inspect.isasyncgenfunction(get_db)


def test_engine_is_lazy_and_cached():
    assert get_engine() is get_engine()
    assert get_engine().url == make_url(get_settings().database_url)


async def test_check_database_raises_when_unreachable(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pw@127.0.0.1:1/absent")
    get_settings.cache_clear()
    await close_database()
    try:
        with pytest.raises(UNREACHABLE):
            await check_database()
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
        await close_database()


async def test_get_db_commits_when_the_request_finishes(db, owner):
    """The dependency owns one transaction per request: clean exit commits."""
    from app.models.tenant import Tenant
    from tests.factories import create_tenant

    tenant_id = await create_tenant(owner, name="Acme", slug="acme")

    generator = get_db()
    session = await anext(generator)
    try:
        assert isinstance(session, AsyncSession)
        assert (await session.execute(text("SELECT 1"))).fetchone()[0] == 1
    finally:
        with pytest.raises(StopAsyncIteration):
            await generator.asend(None)

    async with owner() as verify, verify.begin():
        assert await verify.get(Tenant, tenant_id) is not None


async def test_get_db_rolls_back_when_the_request_fails(db, owner):
    from app.models.tenant import Tenant
    from tests.factories import create_tenant

    tenant_id = await create_tenant(owner, name="Acme", slug="acme")

    generator = get_db()
    session = await anext(generator)
    tenant = await session.get(Tenant, tenant_id)
    tenant.name = "Mutated but never committed"

    with pytest.raises(RuntimeError):
        await generator.athrow(RuntimeError("business failure"))

    async with owner() as verify, verify.begin():
        assert (await verify.get(Tenant, tenant_id)).name == "Acme"
