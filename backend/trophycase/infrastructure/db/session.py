"""Async SQLAlchemy engine/session management.

The engine is created lazily so importing the package never touches the network - a
requirement for ``alembic --help``, unit tests and ``flet run`` on a client machine.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlmodel import SQLModel

import trophycase.infrastructure.db.models  # noqa: F401  (registers every table for init_db)
from trophycase.config import get_settings

__all__ = [
    "dispose_engine",
    "get_engine",
    "get_session",
    "get_session_factory",
    "init_db",
    "session_scope",
    "set_engine_url",
]


_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None
_engine_url: str | None = None


def set_engine_url(url: str) -> None:
    """Point the session machinery at a different database (tests use SQLite).

    The previous engine is *not* disposed here because this is a synchronous helper;
    call ``await dispose_engine()`` first when swapping a live engine.
    """
    global _engine, _factory, _engine_url
    _engine_url = url
    _engine = None
    _factory = None


def get_engine() -> AsyncEngine:
    """Return (creating on first use) the process-wide async engine."""
    global _engine
    if _engine is None:
        url = _engine_url or get_settings().database_url
        kwargs: dict[str, Any] = {"pool_pre_ping": True, "echo": get_settings().database_echo}
        if url.startswith("sqlite"):
            kwargs["poolclass"] = None
        else:
            kwargs.update(pool_size=10, max_overflow=20, pool_recycle=1800)
        _engine = create_async_engine(url, **kwargs)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the session factory bound to :func:`get_engine`."""
    global _factory
    if _factory is None:
        _factory = async_sessionmaker(get_engine(), expire_on_commit=False)
    return _factory


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one session per request, committed when the handler succeeds.

    Every write the API performs happens through this dependency, so committing here (and
    rolling back on any exception) is what makes a request atomic.
    """
    async with get_session_factory()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
        else:
            await session.commit()


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope used by the worker: commit on success, rollback on error."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db() -> None:
    """Create tables from metadata (development/test convenience; Alembic owns prod)."""
    async with get_engine().begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)


async def dispose_engine() -> None:
    """Close the engine (lifespan shutdown)."""
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _factory = None
