"""Sync engine: optimistic local writes, idempotent push and catalogue reconciliation."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from trophycase_client.api import ApiClient, ApiError
from trophycase_client.offline.store import AchievementRow, LocalStore, utc_iso
from trophycase_client.offline.sync import SyncEngine

COUNTER_RULE: dict[str, Any] = {
    "kind": "counter",
    "event_type": "task.completed",
    "count": 5,
}


def make_row(
    *,
    current: float,
    achievement_id: str = "a1",
    rule: dict[str, Any] | None = None,
) -> AchievementRow:
    return AchievementRow(
        id=achievement_id,
        slug="first-steps",
        title="First steps",
        description="Five tasks",
        icon="trophy",
        points=10,
        rule=rule if rule is not None else COUNTER_RULE,
        state={
            "count": int(current),
            "value": 0.0,
            "streak": 0,
            "last_day": None,
        },
        current=current,
        target=5.0,
        progress=current / 5,
        unlocked=current >= 5,
        unlocked_at=None,
    )


def server_row(current: float, *, achievement_id: str = "a1") -> dict[str, Any]:
    row = make_row(current=current, achievement_id=achievement_id)
    payload = row.to_json()
    payload["rule"] = row.rule
    payload["state"] = row.state
    return payload


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LocalStore]:
    local = LocalStore(tmp_path / "client.db")
    yield local
    local.close()


class FakeApi:
    """Minimal stand-in for :class:`ApiClient` (same call surface, no network)."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, *, offline: bool = False):
        self.rows = rows or []
        self.offline = offline
        self.pushed: list[list[dict[str, Any]]] = []
        self.pulled: list[str | None] = []
        self.status = "applied"

    async def push_ops(self, ops: list[dict[str, Any]]) -> dict[str, Any]:
        if self.offline:
            raise ApiError(503, "unavailable", "server down")
        self.pushed.append(ops)
        return {
            "results": [
                {"op_id": op["op_id"], "status": self.status, "conflict": False, "detail": {}}
                for op in ops
            ],
            "applied": len(ops),
            "duplicates": 0,
            "conflicts": 0,
            "server_time": utc_iso(),
        }

    async def catalogue(self) -> dict[str, Any]:
        if self.offline:
            raise ApiError(503, "unavailable", "server down")
        return {
            "achievements": self.rows,
            "unlocked": sum(1 for row in self.rows if row["unlocked"]),
            "score": 0,
            "generated_at": utc_iso(),
        }

    async def pull_changes(self, since: str | None, limit: int = 200) -> dict[str, Any]:
        self.pulled.append(since)
        return {
            "cursor": "2026-10-04T00:00:00Z",
            "server_time": utc_iso(),
            "has_more": False,
            "events": [],
        }


def engine_for(store: LocalStore, api: FakeApi) -> SyncEngine:
    return SyncEngine(store, cast(ApiClient, api))


def test_record_event_is_optimistic_and_queued(store: LocalStore) -> None:
    store.replace_catalogue([make_row(current=0.0)])
    engine = engine_for(store, FakeApi())

    recorded = engine.record_event(
        event_type="task.completed",
        payload={},
        occurred_at=datetime.now(UTC),
    )

    assert store.pending_count() == 1
    assert recorded.op_id == store.pending()[0].op_id
    assert [row.current for row in recorded.changed] == [1.0]
    assert store.list_achievements()[0].current == 1.0


def test_flush_pushes_every_op_and_clears_the_outbox(store: LocalStore) -> None:
    store.queue_event(event_type="task.completed", payload={"n": 1})
    store.queue_event(event_type="task.completed", payload={"n": 2})
    api = FakeApi(rows=[server_row(0.0)])

    outcome = asyncio.run(engine_for(store, api).flush())

    assert outcome.ok
    assert outcome.pushed == 2
    assert outcome.applied == 2
    assert store.pending_count() == 0
    assert len(api.pushed) == 1
    assert len(api.pushed[0]) == 2


def test_flush_keeps_the_outbox_when_the_server_is_down(store: LocalStore) -> None:
    store.queue_event(event_type="task.completed", payload={})
    engine = engine_for(store, FakeApi(offline=True))

    outcome = asyncio.run(engine.flush())

    assert not outcome.ok
    assert not outcome.online
    assert store.pending_count() == 1
    assert store.pending()[0].attempts == 1
    assert store.pending()[0].last_error == "server down"


def test_flush_merges_when_the_worker_has_not_caught_up(store: LocalStore) -> None:
    store.replace_catalogue([make_row(current=3.0)])
    engine = engine_for(store, FakeApi(rows=[server_row(1.0)]))

    outcome = asyncio.run(engine.flush())

    assert outcome.ok
    row = store.list_achievements()[0]
    assert row.current == 3.0  # optimistic progress survives a lagging server
    assert row.state["count"] == 3


def test_flush_takes_server_truth_when_the_server_is_ahead(store: LocalStore) -> None:
    store.replace_catalogue([make_row(current=1.0)])
    engine = engine_for(store, FakeApi(rows=[server_row(4.0)]))

    asyncio.run(engine.flush())

    row = store.list_achievements()[0]
    assert row.current == 4.0
    assert row.state["count"] == 4


def test_rejection_forces_server_truth(store: LocalStore) -> None:
    store.replace_catalogue([make_row(current=4.0)])
    store.queue_event(event_type="task.completed", payload={})
    api = FakeApi(rows=[server_row(0.0)])
    api.status = "rejected"

    outcome = asyncio.run(engine_for(store, api).flush())

    assert outcome.rejected == 1
    assert store.pending_count() == 0  # a rejected op must not loop forever
    assert store.list_achievements()[0].current == 0.0  # drift healed by the server


def test_cursor_is_persisted_between_pulls(store: LocalStore) -> None:
    asyncio.run(engine_for(store, FakeApi(rows=[])).flush())
    assert store.get_meta("cursor") == "2026-10-04T00:00:00Z"

    api = FakeApi(rows=[])
    asyncio.run(engine_for(store, api).flush())
    assert api.pulled == ["2026-10-04T00:00:00Z"]


def test_unknown_status_keeps_the_op_queued(store: LocalStore) -> None:
    store.queue_event(event_type="task.completed", payload={})
    api = FakeApi(rows=[])
    api.status = "mystery"

    asyncio.run(engine_for(store, api).flush())

    assert store.pending_count() == 1
    assert store.pending()[0].attempts == 1
