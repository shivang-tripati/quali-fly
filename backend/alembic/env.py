"""Alembic environment: async engine, settings-driven URL."""

import asyncio
import logging
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from app.core.config import get_settings
from app.db.base import Base

config = context.config
if config.config_file_name is not None:
    # disable_existing_loggers must be False: the default (True) would silently switch
    # off every application logger, so migrations run in-process would mute the API's
    # structured logs.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

settings = get_settings()
if settings.migration_database_url:
    config.set_main_option("sqlalchemy.url", settings.migration_database_url)
else:
    logging.getLogger("alembic.env").warning(
        "MIGRATION_DATABASE_URL is not set; migrating as the application role. "
        "Row Level Security is bypassed by table owners and superusers, so the "
        "tenant-isolation tests are meaningless in that setup."
    )
    config.set_main_option("sqlalchemy.url", settings.database_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(_do_run_migrations)
    await connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
