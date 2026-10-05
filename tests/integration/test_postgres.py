"""Repository behaviour against a real PostgreSQL server (testcontainers).

These tests prove two things the SQLite suite cannot: that the Alembic migration applies
cleanly to a fresh PostgreSQL database, and that timezone-aware timestamps survive a round
trip through ``TIMESTAMPTZ``.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from testcontainers.community.postgres import PostgresContainer

from trophycase.domain.entities import User, UserStatus
from trophycase.domain.ids import uuid7
from trophycase.infrastructure.db.repositories import UserRepository
from trophycase.infrastructure.db.session import (
    dispose_engine,
    get_session_factory,
    set_engine_url,
)

pytestmark = pytest.mark.integration

_REPO_ROOT = Path(__file__).resolve().parents[2]
_IMAGE = "postgres:16-alpine"


def _new_id() -> UUID:
    return uuid7()


def _now() -> datetime:
    return datetime.now(UTC)


def _docker_available() -> bool:
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        return False
    return True


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    """A throwaway PostgreSQL container, migrated by Alembic before it is handed over."""
    if not _docker_available():
        pytest.skip("Docker daemon is not available")

    with PostgresContainer(_IMAGE) as container:
        # testcontainers hands back a sync-driver URL; the application is async-only.
        raw = container.get_connection_url()
        url = raw.replace(raw.split("://", 1)[0], "postgresql+asyncpg", 1)
        # Never resolve ``localhost`` here: it can answer on ::1 before IPv4 and some
        # Windows COM services accept that socket and then say nothing at all, which
        # looks exactly like a stalled TLS handshake.
        url = url.replace("@localhost:", "@127.0.0.1:", 1)
        env = {**os.environ, "ALEMBIC_DATABASE_URL": url}
        # The migration itself is part of what is under test.
        completed = subprocess.run(
            [sys.executable, "-c", "from alembic.config import main; main()", "upgrade", "head"],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, completed.stdout + completed.stderr
        yield url


@pytest_asyncio.fixture
async def pg_session(postgres_url: str) -> AsyncGenerator[AsyncSession]:
    await dispose_engine()
    set_engine_url(postgres_url)
    try:
        async with get_session_factory()() as session:
            yield session
    finally:
        await dispose_engine()
        set_engine_url("")


async def test_migration_creates_every_table(pg_session: AsyncSession) -> None:
    rows = (
        (
            await pg_session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = 'public' ORDER BY table_name"
                )
            )
        )
        .scalars()
        .all()
    )

    assert list(rows) == [
        "achievements",
        "alembic_version",  # proof the Alembic migration - not create_all - ran
        "device_sessions",
        "friendships",
        "progress_logs",
        "sync_operations",
        "user_achievements",
        "user_events",
        "users",
    ]


async def test_user_round_trip_keeps_utc(pg_session: AsyncSession) -> None:
    user = User(
        id=_new_id(),
        email="integration@example.com",
        display_name="Integration",
        status=UserStatus.ACTIVE,
        created_at=_now(),
        updated_at=_now(),
        password_hash="not-a-real-hash",
    )

    saved = await UserRepository(pg_session).add(user)
    await pg_session.commit()
    loaded = await UserRepository(pg_session).get(saved.id)

    assert loaded is not None
    assert loaded.email == "integration@example.com"
    assert loaded.created_at.tzinfo is not None, "TIMESTAMPTZ must come back timezone aware"
    assert loaded.created_at.utcoffset() == timedelta(0)


async def test_email_uniqueness_is_enforced_by_the_database(pg_session: AsyncSession) -> None:
    repo = UserRepository(pg_session)
    await repo.add(
        User(
            id=_new_id(),
            email="unique@example.com",
            display_name="First",
            status=UserStatus.ACTIVE,
            created_at=_now(),
            updated_at=_now(),
        )
    )
    await pg_session.commit()

    with pytest.raises(IntegrityError):
        await repo.add(
            User(
                id=_new_id(),
                email="unique@example.com",
                display_name="Second",
                status=UserStatus.ACTIVE,
                created_at=_now(),
                updated_at=_now(),
            )
        )
        await pg_session.commit()
    await pg_session.rollback()
