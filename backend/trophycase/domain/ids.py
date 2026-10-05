"""Time-sortable, collision-resistant identifiers.

``uuid7`` produces identifiers whose raw bytes are ordered by creation time, which keeps
PostgreSQL primary keys append-friendly and lets the sync layer order operations without a
separate sequence column.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid

__all__ = ["new_id", "uuid7"]

_RAND_BITS = 74  # rand_a (12) + rand_b (62)
_RAND_MASK = (1 << _RAND_BITS) - 1
_LOCK = threading.Lock()
_state_ms = -1
_state_rand = 0


def uuid7(*, now_ms: int | None = None) -> uuid.UUID:
    """Return a new RFC 9562 UUID version 7.

    Layout: 48-bit unix-ms timestamp | 4-bit version ``0111`` | 12-bit ``rand_a`` |
    2-bit variant ``10`` | 62-bit ``rand_b``.

    Generation is monotonic: within a millisecond the random bits are incremented instead
    of redrawn, so ids created back to back still sort by creation order. A clock that
    stalls or steps backwards reuses the last timestamp until it catches up.
    """
    global _state_ms, _state_rand
    timestamp = int(time.time() * 1000) if now_ms is None else now_ms
    if timestamp < 0 or timestamp >= 2**48:
        raise ValueError("timestamp must fit into 48 bits")

    with _LOCK:
        if timestamp <= _state_ms:
            timestamp = _state_ms
            _state_rand = (_state_rand + 1) & _RAND_MASK
            if _state_rand == 0:  # 74 bits exhausted: step one millisecond forward
                timestamp += 1
                _state_rand = secrets.randbits(_RAND_BITS)
        else:
            _state_rand = secrets.randbits(_RAND_BITS)
        _state_ms = timestamp
        rand = _state_rand

    rand_a = rand >> 62
    rand_b = rand & ((1 << 62) - 1)
    raw = bytearray(timestamp.to_bytes(6, "big"))
    raw += bytes([0x70 | (rand_a >> 8), rand_a & 0xFF, 0x80 | (rand_b >> 56)])
    raw += (rand_b & 0x00FF_FFFF_FFFF_FFFF).to_bytes(7, "big")
    return uuid.UUID(bytes=bytes(raw))


def new_id() -> uuid.UUID:
    """Alias used by repositories when a fresh primary key is needed."""
    return uuid7()
