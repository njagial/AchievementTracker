"""Sync reconciliation rules: idempotent replay, last-writer-wins and pull cursors."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from trophycase.application.auth_service import AuthService
from trophycase.application.sync_service import SyncOp, SyncService
from trophycase.infrastructure.db.repositories import UserRepository
from trophycase.infrastructure.db.session import get_session_factory
from trophycase.infrastructure.event_bus import EventBus

Ctx = tuple[AsyncSession, SyncService, UUID, EventBus]


@pytest_asyncio.fixture
async def ctx(db: None, redis: Any) -> AsyncGenerator[Ctx]:
    """A session holding a freshly registered user plus a ``SyncService`` on top of it."""
    async with get_session_factory()() as session:
        auth = await AuthService(session).register(
            email=f"{uuid4().hex[:12]}@example.com",
            password="correct horse battery",
            display_name="Sync Tester",
            device_id="dev-1",
            device_name="pytest",
        )
        await session.commit()
        yield session, SyncService(session, EventBus()), auth.user.id, EventBus()


def _event_op(**overrides: Any) -> SyncOp:
    fields: dict[str, Any] = {
        "op_id": uuid4(),
        "seq": 1,
        "kind": "event",
        "event_type": "task.completed",
        "payload": {"task_id": "t-1"},
        "occurred_at": datetime.now(UTC),
    }
    fields.update(overrides)
    return SyncOp(**fields)


def _profile_op(base: datetime, name: str = "Renamed", **overrides: Any) -> SyncOp:
    fields: dict[str, Any] = {
        "op_id": uuid4(),
        "seq": 1,
        "kind": "profile",
        "fields": {"display_name": name, "avatar_url": None},
        "base_updated_at": base,
    }
    fields.update(overrides)
    return SyncOp(**fields)


async def _profile_base(ctx: Ctx) -> datetime:
    session, _service, user_id, _bus = ctx
    user = await UserRepository(session).get(user_id)
    assert user is not None
    return user.updated_at


async def test_replaying_an_event_op_is_a_duplicate(ctx: Ctx) -> None:
    _session, service, user_id, _bus = ctx
    op = _event_op()

    first = await service.push(user_id=user_id, client_id="dev-1", ops=[op])
    second = await service.push(user_id=user_id, client_id="dev-1", ops=[op])

    assert first.results[0].status == "applied"
    assert second.results[0].status == "duplicate"
    assert second.results[0].conflict is False


async def test_applied_event_ops_reach_the_evaluation_stream(ctx: Ctx) -> None:
    _session, service, user_id, bus = ctx
    op = _event_op()

    await service.push(user_id=user_id, client_id="dev-1", ops=[op])

    await bus.ensure_consumer_group()
    entries = await bus.read_events("test-consumer", block_ms=1)
    assert [fields["event_id"] for _mid, fields in entries] == [str(op.op_id)]
    assert [fields["event_type"] for _mid, fields in entries] == ["task.completed"]


async def test_profile_op_with_a_current_base_applies(ctx: Ctx) -> None:
    session, service, user_id, _bus = ctx
    base = await _profile_base(ctx)

    pushed = await service.push(user_id=user_id, client_id="dev-1", ops=[_profile_op(base)])
    await session.commit()

    assert pushed.results[0].status == "applied"
    assert pushed.conflicts == 0
    assert pushed.results[0].detail["display_name"] == "Renamed"


async def test_profile_op_with_a_stale_base_conflicts(ctx: Ctx) -> None:
    _session, service, user_id, _bus = ctx
    base = await _profile_base(ctx)
    await service.push(user_id=user_id, client_id="dev-1", ops=[_profile_op(base)])
    await asyncio.sleep(0.01)  # guarantee the server clock moved on

    stale = await service.push(
        user_id=user_id, client_id="dev-1", ops=[_profile_op(base, name="Stale")]
    )

    assert stale.results[0].status == "rejected"
    assert stale.results[0].conflict is True
    assert stale.conflicts == 1
    # The authoritative snapshot comes back with the rejection so the device can converge.
    assert stale.results[0].detail["server"]["display_name"] == "Renamed"


async def test_profile_op_replayed_from_the_journal_is_a_duplicate(ctx: Ctx) -> None:
    _session, service, user_id, _bus = ctx
    op = _profile_op(await _profile_base(ctx))

    await service.push(user_id=user_id, client_id="dev-1", ops=[op])
    replay = await service.push(user_id=user_id, client_id="dev-1", ops=[op])

    assert replay.results[0].status == "duplicate"
    assert replay.applied == 0


async def test_pull_returns_events_after_the_cursor(ctx: Ctx) -> None:
    _session, service, user_id, _bus = ctx
    ops = [_event_op(seq=1), _event_op(seq=2)]
    await service.push(user_id=user_id, client_id="dev-1", ops=ops)

    first = await service.pull(user_id=user_id, since=None)
    assert [event.id for event in first.events] == [ops[0].op_id, ops[1].op_id]
    assert first.cursor == first.events[-1].received_at

    empty = await service.pull(user_id=user_id, since=first.cursor)
    assert empty.events == []


async def test_pull_ignores_other_users_events(ctx: Ctx) -> None:
    session, service, user_id, _bus = ctx
    other = await AuthService(session).register(
        email=f"{uuid4().hex[:12]}@example.com",
        password="correct horse battery",
        display_name="Neighbour",
        device_id="dev-2",
        device_name="pytest",
    )
    await service.push(user_id=user_id, client_id="dev-1", ops=[_event_op()])
    await service.push(user_id=other.user.id, client_id="dev-2", ops=[_event_op()])

    mine = await service.pull(user_id=user_id, since=None)
    theirs = await service.pull(user_id=other.user.id, since=None)

    assert len(mine.events) == 1
    assert len(theirs.events) == 1
    assert mine.events[0].id != theirs.events[0].id


async def test_unknown_operation_kind_is_rejected(ctx: Ctx) -> None:
    _session, service, user_id, _bus = ctx
    op = _event_op(kind="teleport")

    result = await service.push(user_id=user_id, client_id="dev-1", ops=[op])

    assert result.results[0].status == "rejected"
    assert result.results[0].conflict is False
    assert "teleport" in result.results[0].detail["reason"]
