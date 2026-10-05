"""Achievement rule specifications and the pure evaluation engine.

A rule is a declarative, JSON-serialisable description of what has to happen before an
achievement unlocks. Four kinds are supported:

===========  =====================================================================
Kind         Meaning
===========  =====================================================================
``counter``  N matching events (``task.completed`` x 5)
``milestone`` cumulative numeric value taken from the event payload (1000 points)
``boolean``  the very first matching event unlocks it
``streak``   matching events on N consecutive calendar days (7-day login streak)
===========  =====================================================================

Everything here is pure: no I/O, no clock reads beyond the timestamp supplied by the
caller, therefore trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from trophycase.domain.errors import InvalidRuleError

__all__ = [
    "BooleanRule",
    "CounterRule",
    "MilestoneRule",
    "ProgressState",
    "RuleEvaluation",
    "RuleKind",
    "RuleSpec",
    "StreakRule",
    "apply_event",
    "dump_rule",
    "evaluate",
    "parse_rule",
]

RuleKind = Literal["counter", "milestone", "boolean", "streak"]

_RULE_KINDS: tuple[str, ...] = ("counter", "milestone", "boolean", "streak")


class _RuleBase(BaseModel):
    """Shared attributes: which events feed the rule."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    event_type: str = Field(min_length=1, max_length=64, examples=["task.completed"])
    target_id: str | None = Field(
        default=None,
        max_length=128,
        description="Optional payload filter, e.g. the id of a specific task.",
    )


class CounterRule(_RuleBase):
    """Unlock after ``count`` matching events."""

    kind: Literal["counter"] = "counter"
    count: int = Field(ge=1, le=1_000_000)
    comparison: Literal["at_least", "exactly"] = "at_least"


class MilestoneRule(_RuleBase):
    """Unlock once the summed ``value_field`` of matching events reaches ``threshold``."""

    kind: Literal["milestone"] = "milestone"
    threshold: float = Field(gt=0, le=1e12)
    value_field: str = Field(default="value", min_length=1, max_length=64)


class BooleanRule(_RuleBase):
    """Unlock on the first matching event."""

    kind: Literal["boolean"] = "boolean"


class StreakRule(_RuleBase):
    """Unlock after matching events on ``days`` consecutive calendar days (UTC)."""

    kind: Literal["streak"] = "streak"
    days: int = Field(ge=2, le=3660)


type RuleSpec = Annotated[
    CounterRule | MilestoneRule | BooleanRule | StreakRule,
    Field(discriminator="kind"),
]

_RULE_ADAPTER: TypeAdapter[RuleSpec] = TypeAdapter(RuleSpec)


def parse_rule(data: str | bytes | dict[str, Any]) -> RuleSpec:
    """Parse a rule from a JSON string or an already-decoded mapping."""
    try:
        if isinstance(data, str | bytes):
            return _RULE_ADAPTER.validate_json(data)
        return _RULE_ADAPTER.validate_python(data)
    except ValueError as exc:  # pydantic ValidationError
        raise InvalidRuleError(f"invalid achievement rule: {exc}") from exc


def dump_rule(rule: RuleSpec) -> str:
    """Serialise a rule for storage in the ``achievements.rule_spec`` text column."""
    return _RULE_ADAPTER.dump_json(rule).decode("utf-8")


def validate_rule_kind(kind: str) -> None:
    """Reject unknown kinds early (used by the admin/import API)."""
    if kind not in _RULE_KINDS:
        raise InvalidRuleError(
            f"unknown rule kind {kind!r}", details={"allowed": list(_RULE_KINDS)}
        )


# --------------------------------------------------------------------------------------
# Progress state + evaluation
# --------------------------------------------------------------------------------------
@dataclass(frozen=True, slots=True)
class ProgressState:
    """Accumulated progress for one (user, achievement) pair."""

    count: int = 0
    value: float = 0.0
    streak: int = 0
    last_day: date | None = None

    @property
    def has_progress(self) -> bool:
        return self.count > 0 or self.value > 0 or self.streak > 0


@dataclass(frozen=True, slots=True)
class RuleEvaluation:
    """Result of evaluating a rule: progress numbers plus the new state."""

    current: float
    target: float
    completed: bool
    progress: float
    state: ProgressState

    @property
    def just_unlocked(self) -> bool:
        """True only when this evaluation pushed the rule over the line."""
        return self.completed and self.current >= self.target


def _target(rule: RuleSpec) -> float:
    match rule:
        case CounterRule():
            return float(rule.count)
        case MilestoneRule():
            return rule.threshold
        case BooleanRule():
            return 1.0
        case StreakRule():
            return float(rule.days)


def _current(rule: RuleSpec, state: ProgressState) -> float:
    match rule:
        case CounterRule():
            return float(state.count)
        case MilestoneRule():
            return state.value
        case BooleanRule():
            return 1.0 if state.count > 0 else 0.0
        case StreakRule():
            return float(state.streak)


def _is_completed(rule: RuleSpec, current: float, target: float) -> bool:
    match rule:
        case CounterRule(comparison="exactly"):
            return int(current) == int(target)
        case CounterRule():
            return current >= target
        case _:
            return current >= target


def evaluate(rule: RuleSpec, state: ProgressState) -> RuleEvaluation:
    """Evaluate ``rule`` against an existing ``state`` without mutating anything."""
    target = _target(rule)
    current = _current(rule, state)
    progress = 0.0 if target <= 0 else min(1.0, max(0.0, current / target))
    return RuleEvaluation(
        current=current,
        target=target,
        completed=_is_completed(rule, current, target),
        progress=round(progress, 6),
        state=state,
    )


def _matches(rule: RuleSpec, event_type: str, payload: dict[str, Any]) -> bool:
    if event_type != rule.event_type:
        return False
    if rule.target_id is None:
        return True
    raw_target = payload.get("target_id")
    return raw_target is not None and str(raw_target) == rule.target_id


def _numeric(payload: dict[str, Any], field: str) -> float:
    """Read ``payload[field]`` as a float.

    Missing field counts the occurrence as ``1.0``; a present but non-numeric value is
    ignored (``0.0``) so a malformed payload can never unlock a milestone.
    """
    if field not in payload:
        return 1.0
    raw = payload[field]
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return 0.0
    return float(raw)


def _next_streak(state: ProgressState, day: date) -> int:
    if state.last_day is None:
        return 1
    if day < state.last_day:
        return max(state.streak, 1)  # late/back-filled event: keep the run alive
    if day == state.last_day:
        return max(state.streak, 1)
    if day == state.last_day + timedelta(days=1):
        return state.streak + 1
    return 1  # gap -> run restarts


def apply_event(
    rule: RuleSpec,
    state: ProgressState,
    *,
    event_type: str,
    payload: dict[str, Any],
    occurred_at: datetime,
) -> RuleEvaluation:
    """Fold one user event into ``state`` and return the resulting evaluation.

    Events that do not match ``rule`` return the unchanged state, so callers can use the
    return value unconditionally. The function is idempotent for identical inputs only in
    the sense that the *engine* is responsible for de-duplicating replayed events.
    """
    if not _matches(rule, event_type, payload):
        return evaluate(rule, state)

    day = occurred_at.date()
    match rule:
        case CounterRule():
            new_state = replace(state, count=state.count + 1, last_day=day)
        case MilestoneRule():
            new_state = replace(
                state,
                count=state.count + 1,
                value=state.value + _numeric(payload, rule.value_field),
                last_day=day,
            )
        case BooleanRule():
            new_state = replace(state, count=max(state.count, 1), last_day=day)
        case StreakRule():
            new_state = replace(
                state,
                count=state.count + 1,
                streak=_next_streak(state, day),
                last_day=day,
            )
    return evaluate(rule, new_state)
