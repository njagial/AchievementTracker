"""Application state: optimistic writes, toasts and realtime message handling."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from trophycase_client.config import ClientConfig
from trophycase_client.model import AppState
from trophycase_client.offline.store import AchievementRow, LocalStore

RULE: dict[str, Any] = {"kind": "counter", "event_type": "task.completed", "count": 1}


def make_row(*, current: float = 0.0, unlocked: bool = False) -> AchievementRow:
    return AchievementRow(
        id="a1",
        slug="first-task",
        title="First task",
        description="Complete one task",
        icon="check",
        points=5,
        rule=RULE,
        state={"count": int(current), "value": 0.0, "streak": 0, "last_day": None},
        current=current,
        target=1.0,
        progress=current,
        unlocked=unlocked,
        unlocked_at=None,
    )


@pytest.fixture
def state(tmp_path: Path) -> Iterator[AppState]:
    config = ClientConfig(
        api_base_url="http://127.0.0.1:9",
        ws_url="ws://127.0.0.1:9/v1/ws",
        device_name="test-device",
        data_dir=tmp_path,
        verify=True,
    )
    app_state = AppState(config, store=LocalStore(config.database_path))
    yield app_state
    app_state.store.close()


def test_log_event_updates_progress_and_queues_the_write(state: AppState) -> None:
    state.store.replace_catalogue([make_row()])
    state.achievements = state.store.list_achievements()

    state.log_event("task.completed", {})

    assert state.store.pending_count() == 1
    assert state.achievements[0].current == 1.0
    assert state.achievements[0].unlocked


def test_unlock_produces_a_toast(state: AppState) -> None:
    state.store.replace_catalogue([make_row()])

    state.log_event("task.completed", {})

    toast = state.pop_toast()
    assert toast is not None
    assert toast.kind == "unlock"
    assert "First task" in toast.title
    assert state.pop_toast() is None


def test_observer_is_notified(state: AppState) -> None:
    seen: list[int] = []
    unsubscribe = state.subscribe(lambda: seen.append(1))
    state.log_event("task.completed", {})
    assert seen

    unsubscribe()
    state.log_event("task.completed", {})
    assert len(seen) == 1


def test_realtime_progress_message_updates_the_mirror(state: AppState) -> None:
    state.store.replace_catalogue([make_row()])

    import asyncio

    asyncio.run(
        state.handle_message(
            {
                "type": "progress.updated",
                "achievement_id": "a1",
                "current": 1.0,
                "target": 1.0,
                "progress": 1.0,
                "completed": True,
            }
        )
    )

    row = state.store.get_achievement("a1")
    assert row is not None
    assert row.current == 1.0
    assert row.state["count"] == 1


def test_realtime_unlock_message_toasts(state: AppState) -> None:
    state.store.replace_catalogue([make_row()])

    import asyncio

    asyncio.run(
        state.handle_message(
            {
                "type": "achievement.unlocked",
                "achievement_id": "a1",
                "title": "First task",
                "description": "Nice start",
                "points": 5,
                "unlocked_at": "2026-10-04T09:00:00Z",
            }
        )
    )

    row = state.store.get_achievement("a1")
    assert row is not None
    assert row.unlocked
    assert row.unlocked_at == "2026-10-04T09:00:00Z"
    toast = state.pop_toast()
    assert toast is not None
    assert toast.kind == "unlock"


def test_user_survives_a_restart(state: AppState) -> None:
    payload = {"id": "u1", "email": "a@b.c", "display_name": "Ada"}
    state.store.set_meta("user", json.dumps(payload))

    restored = AppState(
        state.config,
        store=LocalStore(state.config.database_path),
    )
    try:
        assert restored.user is not None
        assert restored.display_name == "Ada"
    finally:
        restored.store.close()
