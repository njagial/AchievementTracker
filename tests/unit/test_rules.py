"""Unit tests for the pure achievement rule engine (no I/O, no clock)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from trophycase.domain.errors import InvalidRuleError
from trophycase.domain.rules import (
    ProgressState,
    apply_event,
    dump_rule,
    evaluate,
    parse_rule,
)

_DAY_ONE = date(2026, 1, 1)


def _at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 12, 0, tzinfo=UTC)


def _apply(
    rule: dict[str, Any],
    state: ProgressState,
    *,
    event_type: str | None = None,
    payload: dict[str, Any] | None = None,
    occurred_at: datetime | None = None,
) -> ProgressState:
    evaluation = apply_event(
        parse_rule(rule),
        state,
        event_type=event_type if event_type is not None else str(rule["event_type"]),
        payload=payload or {},
        occurred_at=occurred_at or _at(_DAY_ONE),
    )
    return evaluation.state


def test_rule_roundtrips_through_json() -> None:
    rule: dict[str, Any] = {"kind": "counter", "event_type": "task.completed", "count": 5}
    assert parse_rule(dump_rule(parse_rule(rule))) == parse_rule(rule)


@pytest.mark.parametrize(
    "rule",
    [
        {"kind": "nope", "event_type": "a"},
        {"kind": "counter", "event_type": "a"},  # count missing
        {"kind": "streak", "event_type": "a", "days": 1},  # days < 2
        {"kind": "counter", "event_type": "a", "count": 0},  # count < 1
        {"surprise": True},
    ],
)
def test_invalid_rules_are_rejected(rule: dict[str, Any]) -> None:
    with pytest.raises(InvalidRuleError):
        parse_rule(rule)


def test_counter_unlocks_at_the_target() -> None:
    rule: dict[str, Any] = {"kind": "counter", "event_type": "task.completed", "count": 3}
    state = ProgressState()
    for _ in range(3):
        state = _apply(rule, state)
    evaluation = evaluate(parse_rule(rule), state)
    assert evaluation.current == 3
    assert evaluation.target == 3
    assert evaluation.completed is True
    assert evaluation.progress == 1.0


def test_counter_exactly_comparison_never_goes_past() -> None:
    rule: dict[str, Any] = {
        "kind": "counter",
        "event_type": "task.completed",
        "count": 2,
        "comparison": "exactly",
    }
    state = ProgressState()
    for _ in range(4):
        state = _apply(rule, state)
    assert state.count == 4
    assert evaluate(parse_rule(rule), state).completed is False  # 4 != 2


def test_non_matching_event_leaves_state_untouched() -> None:
    rule: dict[str, Any] = {"kind": "counter", "event_type": "task.completed", "count": 5}
    state = _apply(rule, ProgressState())
    after = _apply(rule, state, event_type="session.started")
    assert after == state


def test_target_id_filter_narrows_the_match() -> None:
    rule: dict[str, Any] = {
        "kind": "boolean",
        "event_type": "task.completed",
        "target_id": "task-42",
    }
    state = _apply(rule, ProgressState(), payload={"target_id": "other"})
    assert state.count == 0
    state = _apply(rule, state, payload={"target_id": "task-42"})
    assert state.count == 1
    assert evaluate(parse_rule(rule), state).completed is True


def test_milestone_sums_the_value_field() -> None:
    rule: dict[str, Any] = {
        "kind": "milestone",
        "event_type": "points.earned",
        "threshold": 100,
        "value_field": "points",
    }
    state = _apply(rule, ProgressState(), payload={"points": 40})
    state = _apply(rule, state, payload={"points": 60})
    assert state.value == 100
    assert evaluate(parse_rule(rule), state).completed is True


def test_milestone_ignores_non_numeric_values() -> None:
    rule: dict[str, Any] = {
        "kind": "milestone",
        "event_type": "points.earned",
        "threshold": 10,
        "value_field": "points",
    }
    state = _apply(rule, ProgressState(), payload={"points": "a lot"})
    assert state.value == 0.0
    assert state.count == 1  # the occurrence still counts


def test_boolean_unlocks_on_the_first_match() -> None:
    rule: dict[str, Any] = {"kind": "boolean", "event_type": "account.verified"}
    state = _apply(rule, ProgressState())
    state = _apply(rule, state)
    assert state.count == 1
    assert evaluate(parse_rule(rule), state).completed is True


def test_streak_grows_on_consecutive_days_and_resets_on_gaps() -> None:
    rule: dict[str, Any] = {"kind": "streak", "event_type": "session.opened", "days": 3}
    state = ProgressState()

    state = _apply(rule, state, occurred_at=_at(date(2026, 3, 1)))
    assert state.streak == 1
    state = _apply(rule, state, occurred_at=_at(date(2026, 3, 1)))
    assert state.streak == 1, "two events on one day are one day of the run"
    state = _apply(rule, state, occurred_at=_at(date(2026, 3, 2)))
    assert state.streak == 2
    state = _apply(rule, state, occurred_at=_at(date(2026, 3, 3)))
    assert state.streak == 3
    assert evaluate(parse_rule(rule), state).completed is True

    state = _apply(rule, state, occurred_at=_at(date(2026, 3, 10)))
    assert state.streak == 1, "a gap restarts the run"


def test_streak_survives_a_back_filled_event() -> None:
    rule: dict[str, Any] = {"kind": "streak", "event_type": "session.opened", "days": 7}
    state = _apply(rule, ProgressState(), occurred_at=_at(date(2026, 4, 2)))
    state = _apply(rule, state, occurred_at=_at(date(2026, 4, 3)))
    late = _apply(rule, state, occurred_at=_at(date(2026, 4, 1)))
    assert late.streak == 2, "an out-of-order event must not break the run"
    assert late.last_day == date(2026, 4, 1)


def test_evaluation_reports_are_pure() -> None:
    rule = parse_rule({"kind": "counter", "event_type": "a", "count": 2})
    state = ProgressState()
    first = evaluate(rule, state)
    second = evaluate(rule, state)
    assert first == second
    assert first.state is state
