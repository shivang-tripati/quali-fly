"""Async SQLAlchemy engine, session factory and request-scoped session dependency."""

from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import get_settings

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(
            settings.database_url,
            echo=settings.db_echo,
            pool_pre_ping=True,
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
        )
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(),
            class_=AsyncSession,
            expire_on_commit=False,
        )
    return _session_factory


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session *and one transaction* per request.

    Owning the transaction here is what makes `SET LOCAL` in
    `app.db.tenant_context` safe: the tenant context is created and destroyed with
    the transaction, so a pooled connection can never carry it to the next request.
    The transaction commits when the request completes and rolls back on error.
    """
    async with get_session_factory()() as session:
        async with session.begin():
            yield session


async def check_database() -> None:
    """Round-trip the database. Raises SQLAlchemyError/OperationalError when down."""
    async with get_engine().connect() as connection:
        await connection.execute(text("SELECT 1"))


async def close_database() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


__all__ = [
    "check_database",
    "close_database",
    "get_db",
    "get_engine",
    "get_session_factory",
]
