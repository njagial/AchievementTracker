"""Shared fixtures: a fresh SQLite database, a fake Redis and an ASGI test client.

The environment block below must run before the first ``get_settings()`` call anywhere in
the process, which is why it sits at import time rather than inside a fixture.
"""

from __future__ import annotations

import os
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import Any
from uuid import UUID, uuid4

os.environ.setdefault("TROPHYCASE_ENV", "test")
os.environ.setdefault("TROPHYCASE_DEBUG", "false")
# A single test file makes hundreds of requests; the limiter must not throttle them.
os.environ.setdefault("TROPHYCASE_RATE_LIMIT_PER_MINUTE", "1000000")
os.environ.setdefault("TROPHYCASE_RATE_LIMIT_LOGIN_PER_MINUTE", "1000000")

import fakeredis.aioredis
import httpx
import pytest
import pytest_asyncio

from trophycase.config import reset_settings
from trophycase.infrastructure.db.models import UserEventTable
from trophycase.infrastructure.db.session import (
    dispose_engine,
    get_session_factory,
    init_db,
    set_engine_url,
)
from trophycase.infrastructure.event_bus import EventBus, set_redis

reset_settings()

__all__ = [
    "auth_headers",
    "client",
    "db",
    "headers",
    "published_visibility",
    "redis",
    "register",
    "registered_user",
]

AuthHeaders = dict[str, str]


@pytest_asyncio.fixture
async def db(tmp_path: Any) -> AsyncGenerator[None]:
    """Point the process-wide engine at an empty, file-backed SQLite database."""
    await dispose_engine()
    set_engine_url(f"sqlite+aiosqlite:///{tmp_path.as_posix()}/trophycase.db")
    await init_db()
    yield
    await dispose_engine()
    set_engine_url("")


@pytest_asyncio.fixture
async def redis() -> AsyncGenerator[Any]:
    """Replace the process-wide Redis client with an in-memory fake."""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    set_redis(client)
    yield client
    await client.aclose()
    set_redis(None)


@pytest_asyncio.fixture
async def client(db: None, redis: Any) -> AsyncGenerator[httpx.AsyncClient]:
    """An ``httpx`` client speaking ASGI to a freshly built application."""
    from trophycase.main import create_app

    transport = httpx.ASGITransport(app=create_app())
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http:
        yield http


@pytest_asyncio.fixture
async def register(
    client: httpx.AsyncClient,
) -> AsyncGenerator[Callable[..., Awaitable[dict[str, Any]]]]:
    """Register a new account and return its parsed ``AuthOut`` payload."""

    async def _register(
        *, email: str | None = None, password: str = "correct horse battery", **extra: Any
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "email": email or f"{uuid4().hex[:12]}@example.com",
            "password": password,
            "display_name": extra.pop("display_name", "Test User"),
            **extra,
        }
        response = await client.post("/v1/auth/register", json=body)
        assert response.status_code == 201, response.text
        payload: dict[str, Any] = response.json()
        return payload

    yield _register


@pytest_asyncio.fixture
async def registered_user(register: Callable[..., Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    """A signed-in account with its bearer headers ready to use."""
    return await register()


def auth_headers(payload: dict[str, Any]) -> AuthHeaders:
    """Build the authorization headers for a registered payload."""
    return {"Authorization": f"Bearer {payload['access_token']}"}


@pytest.fixture
def published_visibility(monkeypatch: pytest.MonkeyPatch) -> list[bool]:
    """Record whether a stream message went out *after* its row was committed.

    The worker looks the id up the moment it arrives, so publishing inside the request's
    own transaction is a race the API must never run. One entry per ``push_event``.
    """
    seen: list[bool] = []
    publish = EventBus.push_event

    async def _probe(self: EventBus, *, event_id: UUID, user_id: UUID, event_type: str) -> str:
        async with get_session_factory()() as session:
            row = await session.get(UserEventTable, event_id)
        seen.append(row is not None)
        return await publish(self, event_id=event_id, user_id=user_id, event_type=event_type)

    monkeypatch.setattr(EventBus, "push_event", _probe)
    return seen


@pytest.fixture
def headers() -> Callable[[dict[str, Any]], AuthHeaders]:
    """Fixture wrapper around :func:`auth_headers`."""
    return auth_headers
