"""UUIDv7 helpers: correct version/variant, collision resistance and monotonic order."""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterator

import pytest

import trophycase.domain.ids as ids
from trophycase.domain.ids import uuid7


@pytest.fixture(autouse=True)
def fresh_generator_state() -> Iterator[None]:
    """Isolate the module-level monotonic counter from other tests in the process."""

    def _reset() -> None:
        ids._state_ms = -1
        ids._state_rand = 0

    _reset()
    yield
    _reset()


def _time_ms(value: uuid.UUID) -> int:
    """Extract the 48-bit unix-ms timestamp laid out in bytes 0-5 of a UUIDv7."""
    return value.int >> 80


def test_uuid7_carries_the_version_and_variant_nibbles() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_uuid7_is_monotonic_across_a_burst() -> None:
    generated = [uuid7() for _ in range(500)]
    assert generated == sorted(generated), "ids created back to back sort by creation order"
    assert len(set(generated)) == len(generated), "monotonic must still mean unique"


def test_uuid7_timestamp_is_the_supplied_millisecond() -> None:
    assert _time_ms(uuid7(now_ms=1_700_000_000_000)) == 1_700_000_000_000


def test_uuid7_ignores_a_clock_step_backwards() -> None:
    forward = uuid7(now_ms=1_700_000_000_100)
    stale = uuid7(now_ms=1_700_000_000_000)
    assert stale > forward, "a backwards clock step must not reorder ids"
    assert _time_ms(stale) == _time_ms(forward)


def test_uuid7_uses_wall_clock_time_by_default() -> None:
    before = int(time.time() * 1000)
    value = uuid7()
    after = int(time.time() * 1000)
    assert before <= _time_ms(value) <= after


def test_uuid7_rejects_timestamps_that_do_not_fit_48_bits() -> None:
    for bad in (-1, 2**48):
        with pytest.raises(ValueError, match="48 bits"):
            uuid7(now_ms=bad)
