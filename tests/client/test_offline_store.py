"""Offline store: outbox ordering, optimistic rule evaluation and state healing."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from trophycase_client.offline.store import AchievementRow, LocalStore, utc_iso

COUNTER_RULE: dict[str, Any] = {
    "kind": "counter",
    "event_type": "task.completed",
    "count": 3,
}
MILESTONE_RULE: dict[str, Any] = {
    "kind": "milestone",
    "event_type": "points.earned",
    "threshold": 100,
    "value_field": "value",
}
STREAK_RULE: dict[str, Any] = {
    "kind": "streak",
    "event_type": "app.opened",
    "days": 3,
}


@pytest.fixture
def store(tmp_path: Path) -> Iterator[LocalStore]:
    local = LocalStore(tmp_path / "client.db")
    yield local
    local.close()


def make_row(achievement_id: str = "a1", **overrides: Any) -> AchievementRow:
    base: dict[str, Any] = {
        "id": achievement_id,
        "slug": f"slug-{achievement_id}",
        "title": "First steps",
        "description": "Complete three tasks",
        "icon": "trophy",
        "points": 10,
        "rule": COUNTER_RULE,
        "state": {"count": 0, "value": 0.0, "streak": 0, "last_day": None},
        "current": 0.0,
        "target": 3.0,
        "progress": 0.0,
        "unlocked": False,
        "unlocked_at": None,
    }
    base.update(overrides)
    return AchievementRow(**base)


def test_outbox_is_fifo_and_wire_shaped(store: LocalStore) -> None:
    first = store.queue_event(event_type="task.completed", payload={"id": 1})
    second = store.queue_event(event_type="task.completed", payload={"id": 2})

    pending = store.pending()
    assert [op.op_id for op in pending] == [first, second]
    assert [op.seq for op in pending] == sorted(op.seq for op in pending)

    wire = pending[0].to_wire()
    assert wire["op_id"] == first
    assert wire["kind"] == "event"
    assert wire["event_type"] == "task.completed"
    assert wire["payload"] == {"id": 1}
    assert json.loads(json.dumps(wire)) == wire  # JSON round-trippable


def test_pending_count_and_removal(store: LocalStore) -> None:
    op = store.queue_event(event_type="task.completed", payload={})
    assert store.pending_count() == 1
    store.delete_ops([op])
    assert store.pending_count() == 0


def test_profile_ops_collapse_to_the_newest(store: LocalStore) -> None:
    store.queue_profile({"display_name": "old"}, "2026-01-01T00:00:00Z")
    store.queue_profile({"display_name": "new"}, "2026-01-02T00:00:00Z")
    store.drop_empty_profiles()

    pending = store.pending()
    assert len(pending) == 1
    assert pending[0].kind == "profile"
    assert pending[0].fields["display_name"] == "new"


def test_offline_counter_unlocks_at_threshold(store: LocalStore) -> None:
    store.replace_catalogue([make_row()])

    changed = store.apply_local_event(
        event_type="task.completed",
        payload={},
        occurred_at=datetime.now(UTC),
    )
    assert len(changed) == 1
    row = store.list_achievements()[0]
    assert row.current == 1.0
    assert not row.unlocked

    for _ in range(2):
        store.apply_local_event(
            event_type="task.completed",
            payload={},
            occurred_at=datetime.now(UTC),
        )
    row = store.list_achievements()[0]
    assert row.current == 3.0
    assert row.progress == 1.0
    assert row.unlocked
    assert row.state["count"] == 3


def test_non_matching_event_leaves_state_alone(store: LocalStore) -> None:
    store.replace_catalogue([make_row()])
    changed = store.apply_local_event(
        event_type="habit.logged",
        payload={},
        occurred_at=datetime.now(UTC),
    )
    assert changed == []
    assert store.list_achievements()[0].current == 0.0


def test_milestone_sums_the_payload_value(store: LocalStore) -> None:
    store.replace_catalogue([make_row(rule=MILESTONE_RULE, target=100.0)])
    when = datetime.now(UTC)
    for value in (30, 30, 50):
        store.apply_local_event(
            event_type="points.earned",
            payload={"value": value},
            occurred_at=when,
        )
    row = store.list_achievements()[0]
    assert row.current == 110.0
    assert row.unlocked


def test_streak_counts_consecutive_days(store: LocalStore) -> None:
    store.replace_catalogue([make_row(rule=STREAK_RULE, target=3.0)])
    for day in (date(2026, 10, 1), date(2026, 10, 2), date(2026, 10, 3)):
        store.apply_local_event(
            event_type="app.opened",
            payload={},
            occurred_at=datetime(day.year, day.month, day.day, tzinfo=UTC),
        )
    row = store.list_achievements()[0]
    assert row.state["streak"] == 3
    assert row.unlocked

    # A gap restarts the run rather than freezing it.
    store.apply_local_event(
        event_type="app.opened",
        payload={},
        occurred_at=datetime(2026, 10, 10, tzinfo=UTC),
    )
    assert store.list_achievements()[0].state["streak"] == 1


def test_update_progress_resyncs_the_accumulator(store: LocalStore) -> None:
    store.replace_catalogue([make_row()])
    store.update_progress("a1", current=5.0, target=6.0, progress=5 / 6)

    row = store.list_achievements()[0]
    assert row.state["count"] == 5  # re-derived from the rule kind, not left at 0

    store.apply_local_event(
        event_type="task.completed",
        payload={},
        occurred_at=datetime.now(UTC),
    )
    assert store.list_achievements()[0].state["count"] == 6


def test_update_progress_marks_unlock_with_timestamp(store: LocalStore) -> None:
    store.replace_catalogue([make_row()])
    store.update_progress(
        "a1",
        current=3.0,
        target=3.0,
        progress=1.0,
        completed=True,
        unlocked_at="2026-10-04T12:00:00Z",
    )
    row = store.list_achievements()[0]
    assert row.unlocked
    assert row.unlocked_at == "2026-10-04T12:00:00Z"


def test_feed_is_deduplicated(store: LocalStore) -> None:
    event = {
        "id": "e1",
        "event_type": "task.completed",
        "payload": {},
        "occurred_at": utc_iso(),
        "received_at": utc_iso(),
    }
    assert store.append_feed([event, event]) == 1
    assert store.append_feed([event]) == 0
    assert len(store.feed()) == 1


def test_device_id_is_stable(store: LocalStore) -> None:
    first = store.device_id()
    assert store.device_id() == first
    assert len(first) == 32
