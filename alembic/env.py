"""Alembic environment.

Migrations run on the same async engine and the same settings the application uses, so
``alembic upgrade head`` and the API always talk to one database. Override the target with
``ALEMBIC_DATABASE_URL`` when migrating something other than the default.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlmodel import SQLModel

import trophycase.infrastructure.db.models  # noqa: F401  (registers every table)

config = context.config
target_metadata = SQLModel.metadata


def _database_url() -> str:
    return (
        os.getenv("ALEMBIC_DATABASE_URL")
        or config.get_main_option("sqlalchemy.url")
        or _settings_url()
    )


def _settings_url() -> str:
    from trophycase.config import get_settings

    return get_settings().database_url


def run_migrations_offline() -> None:
    """Emit SQL without a live connection (``alembic upgrade head --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # ``batch_alter_table`` lets the generated SQL run on SQLite as well as
        # PostgreSQL, which keeps development databases and tests in step with prod.
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Run migrations against a live database."""
    connectable = config.attributes.get("sqlalchemy.connection")

    if connectable is None:
        engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
        async with engine.connect() as connection:
            await connection.run_sync(_do_run_migrations)
        await engine.dispose()
    else:
        await connectable.run_sync(_do_run_migrations)


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
