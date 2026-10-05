"""Offline store: SQLite outbox, cached catalogue and last-known server state.

Everything here is synchronous by design. The client only ever touches a few hundred
rows at a time, SQLite commits are sub-millisecond on local disk, and a plain
``sqlite3`` connection keeps the offline path free of any I/O dependency.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trophycase.domain.errors import InvalidRuleError
from trophycase.domain.rules import ProgressState, apply_event, parse_rule

__all__ = [
    "AchievementRow",
    "LocalStore",
    "OutboxOp",
    "utc_iso",
]

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    op_id           TEXT PRIMARY KEY,
    seq             INTEGER NOT NULL,
    kind            TEXT NOT NULL CHECK (kind IN ('event', 'profile')),
    event_type      TEXT,
    payload         TEXT NOT NULL DEFAULT '{}',
    occurred_at     TEXT,
    fields          TEXT NOT NULL DEFAULT '{}',
    base_updated_at TEXT,
    created_at      TEXT NOT NULL,
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);
CREATE INDEX IF NOT EXISTS ix_outbox_seq ON outbox (seq);

CREATE TABLE IF NOT EXISTS achievements (
    id          TEXT PRIMARY KEY,
    slug        TEXT NOT NULL,
    title       TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    icon        TEXT NOT NULL DEFAULT 'trophy',
    points      INTEGER NOT NULL DEFAULT 10,
    rule        TEXT NOT NULL DEFAULT '{}',
    state       TEXT NOT NULL DEFAULT '{}',
    current     REAL NOT NULL DEFAULT 0,
    target      REAL NOT NULL DEFAULT 1,
    progress    REAL NOT NULL DEFAULT 0,
    unlocked    INTEGER NOT NULL DEFAULT 0,
    unlocked_at TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feed (
    id           TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    occurred_at  TEXT NOT NULL,
    received_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_feed_occurred ON feed (occurred_at);
"""


def utc_iso(value: datetime | None = None) -> str:
    """Serialise a datetime as an offset-aware UTC ISO-8601 string."""
    moment = value or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    normalised = value.replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalised)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


@dataclass(frozen=True, slots=True)
class OutboxOp:
    """One queued write waiting for connectivity."""

    op_id: str
    seq: int
    kind: str
    event_type: str | None
    payload: dict[str, Any]
    occurred_at: str | None
    fields: dict[str, Any]
    base_updated_at: str | None
    created_at: str
    attempts: int
    last_error: str | None

    def to_wire(self) -> dict[str, Any]:
        """Shape the row exactly like ``SyncOpIn`` on the server."""
        return {
            "op_id": self.op_id,
            "seq": self.seq,
            "kind": self.kind,
            "event_type": self.event_type,
            "payload": self.payload,
            "occurred_at": self.occurred_at,
            "fields": self.fields,
            "base_updated_at": self.base_updated_at,
        }


@dataclass(slots=True)
class AchievementRow:
    """Cached catalogue entry plus the accumulator used for offline evaluation."""

    id: str
    slug: str
    title: str
    description: str
    icon: str
    points: int
    rule: dict[str, Any]
    state: dict[str, Any]
    current: float
    target: float
    progress: float
    unlocked: bool
    unlocked_at: str | None
    updated_at: str = field(default_factory=utc_iso)

    @property
    def progress_state(self) -> ProgressState:
        raw = self.state or {}
        last_day_raw = raw.get("last_day")
        last_day = _parse_iso(last_day_raw + "T00:00:00Z" if last_day_raw else None)
        return ProgressState(
            count=int(raw.get("count", 0)),
            value=float(raw.get("value", 0.0)),
            streak=int(raw.get("streak", 0)),
            last_day=last_day.date() if last_day else None,
        )

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


class LocalStore:
    """SQLite-backed offline database (outbox + catalogue mirror + activity feed)."""

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # --- meta ----------------------------------------------------------------------
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self._conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return str(row["value"]) if row is not None else default

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self._conn.commit()

    def device_id(self) -> str:
        """Stable per-install id, created on first run."""
        existing = self.get_meta("device_id")
        if existing:
            return existing
        created = uuid.uuid4().hex[:32]
        self.set_meta("device_id", created)
        return created

    # --- outbox --------------------------------------------------------------------
    def _next_seq(self) -> int:
        row = self._conn.execute("SELECT COALESCE(MAX(seq), 0) + 1 AS seq FROM outbox").fetchone()
        return int(row["seq"]) if row is not None else 1

    def queue_event(
        self,
        *,
        event_type: str,
        payload: dict[str, Any],
        occurred_at: datetime | None = None,
        op_id: str | None = None,
    ) -> str:
        """Append an event to the outbox; returns the ``op_id`` used for replay."""
        op = op_id or str(uuid.uuid4())
        self._conn.execute(
            "INSERT INTO outbox (op_id, seq, kind, event_type, payload, occurred_at, fields, "
            "created_at) VALUES (?, ?, 'event', ?, ?, ?, '{}', ?)",
            (
                op,
                self._next_seq(),
                event_type,
                json.dumps(payload, separators=(",", ":")),
                utc_iso(occurred_at),
                utc_iso(),
            ),
        )
        self._conn.commit()
        return op

    def queue_profile(self, fields: dict[str, Any], base_updated_at: datetime | str | None) -> str:
        """Queue a profile write with the server timestamp it was based on (LWW)."""
        op = str(uuid.uuid4())
        base = base_updated_at if isinstance(base_updated_at, str) else utc_iso(base_updated_at)
        self._conn.execute(
            "INSERT INTO outbox (op_id, seq, kind, event_type, payload, occurred_at, fields, "
            "base_updated_at, created_at) VALUES (?, ?, 'profile', NULL, '{}', NULL, ?, ?, ?)",
            (op, self._next_seq(), json.dumps(fields, separators=(",", ":")), base, utc_iso()),
        )
        self._conn.commit()
        return op

    def pending(self, limit: int = 200) -> list[OutboxOp]:
        rows = self._conn.execute("SELECT * FROM outbox ORDER BY seq LIMIT ?", (limit,)).fetchall()
        return [
            OutboxOp(
                op_id=str(row["op_id"]),
                seq=int(row["seq"]),
                kind=str(row["kind"]),
                event_type=row["event_type"],
                payload=json.loads(row["payload"]),
                occurred_at=row["occurred_at"],
                fields=json.loads(row["fields"]),
                base_updated_at=row["base_updated_at"],
                created_at=str(row["created_at"]),
                attempts=int(row["attempts"]),
                last_error=row["last_error"],
            )
            for row in rows
        ]

    def pending_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS n FROM outbox").fetchone()
        return int(row["n"]) if row is not None else 0

    def mark_failed(self, op_id: str, error: str) -> None:
        self._conn.execute(
            "UPDATE outbox SET attempts = attempts + 1, last_error = ? WHERE op_id = ?",
            (error[:500], op_id),
        )
        self._conn.commit()

    def delete_ops(self, op_ids: Iterable[str]) -> None:
        self._conn.executemany("DELETE FROM outbox WHERE op_id = ?", [(op,) for op in op_ids])
        self._conn.commit()

    def drop_empty_profiles(self) -> None:
        """Profile ops are last-writer-wins: only the newest one matters."""
        self._conn.execute(
            "DELETE FROM outbox WHERE kind = 'profile' AND op_id NOT IN ("
            "  SELECT op_id FROM outbox WHERE kind = 'profile' ORDER BY seq DESC LIMIT 1)"
        )
        self._conn.commit()

    # --- catalogue -----------------------------------------------------------------
    def replace_catalogue(self, achievements: list[AchievementRow]) -> None:
        """Overwrite the mirror with server truth (authoritative snapshot)."""
        self._conn.execute("DELETE FROM achievements")
        self._conn.executemany(
            "INSERT INTO achievements (id, slug, title, description, icon, points, rule, state, "
            "current, target, progress, unlocked, unlocked_at, updated_at) "
            "VALUES (:id, :slug, :title, :description, :icon, :points, :rule, :state, :current, "
            ":target, :progress, :unlocked, :unlocked_at, :updated_at)",
            [
                {
                    **row.to_json(),
                    "rule": json.dumps(row.rule, separators=(",", ":")),
                    "state": json.dumps(row.state, separators=(",", ":")),
                    "unlocked": int(row.unlocked),
                }
                for row in achievements
            ],
        )
        self._conn.commit()

    def list_achievements(self) -> list[AchievementRow]:
        rows = self._conn.execute("SELECT * FROM achievements ORDER BY points DESC").fetchall()
        return [self._row_to_achievement(row) for row in rows]

    @staticmethod
    def _row_to_achievement(row: sqlite3.Row) -> AchievementRow:
        return AchievementRow(
            id=str(row["id"]),
            slug=str(row["slug"]),
            title=str(row["title"]),
            description=str(row["description"]),
            icon=str(row["icon"]),
            points=int(row["points"]),
            rule=json.loads(row["rule"]),
            state=json.loads(row["state"]),
            current=float(row["current"]),
            target=float(row["target"]),
            progress=float(row["progress"]),
            unlocked=bool(row["unlocked"]),
            unlocked_at=row["unlocked_at"],
            updated_at=str(row["updated_at"]),
        )

    def apply_local_event(
        self, *, event_type: str, payload: dict[str, Any], occurred_at: datetime
    ) -> list[AchievementRow]:
        """Optimistically fold an event into the cached catalogue.

        The very same pure rule engine the server runs is used here, so the numbers a
        user sees offline are exactly the numbers the server would produce. Returns the
        achievements whose state actually changed (so the UI can announce unlocks).
        """
        changed: list[AchievementRow] = []
        for row in self.list_achievements():
            if not row.rule:
                continue
            try:
                rule = parse_rule(row.rule)
            except InvalidRuleError:
                logger.debug("skipping achievement %s: unreadable rule", row.slug)
                continue
            before = row.progress_state
            evaluation = apply_event(
                rule,
                before,
                event_type=event_type,
                payload=payload,
                occurred_at=occurred_at,
            )
            if evaluation.state == before:
                continue
            row.state = {
                "count": evaluation.state.count,
                "value": evaluation.state.value,
                "streak": evaluation.state.streak,
                "last_day": evaluation.state.last_day.isoformat()
                if evaluation.state.last_day
                else None,
            }
            row.current = evaluation.current
            row.target = evaluation.target
            row.progress = evaluation.progress
            if evaluation.completed and not row.unlocked:
                row.unlocked = True
                row.unlocked_at = utc_iso(occurred_at)
            row.updated_at = utc_iso()
            self._conn.execute(
                "UPDATE achievements SET state = ?, current = ?, target = ?, progress = ?, "
                "unlocked = ?, unlocked_at = ?, updated_at = ? WHERE id = ?",
                (
                    json.dumps(row.state, separators=(",", ":")),
                    row.current,
                    row.target,
                    row.progress,
                    int(row.unlocked),
                    row.unlocked_at,
                    row.updated_at,
                    row.id,
                ),
            )
            changed.append(row)
        self._conn.commit()
        return changed

    # --- realtime updates ----------------------------------------------------------
    def update_progress(
        self,
        achievement_id: str,
        *,
        current: float,
        target: float,
        progress: float,
        completed: bool = False,
        unlocked_at: str | None = None,
    ) -> AchievementRow | None:
        """Apply a ``progress.updated`` / ``achievement.unlocked`` push.

        The accumulator stored alongside the display numbers is re-derived from the
        achievement's rule kind, so a later local event folds onto the *new* count
        instead of resurrecting a stale one.
        """
        row = self.get_achievement(achievement_id)
        if row is None:
            return None
        kind = str((row.rule or {}).get("kind", ""))
        state = dict(row.state or {})
        if kind == "counter":
            state["count"] = int(current)
        elif kind == "boolean":
            state["count"] = 1 if current >= 1 else 0
        elif kind == "milestone":
            state["value"] = float(current)
        elif kind == "streak":
            state["streak"] = int(current)
        row.state = state
        row.current = current
        row.target = target
        row.progress = progress
        if completed and not row.unlocked:
            row.unlocked = True
            row.unlocked_at = unlocked_at or utc_iso()
        row.updated_at = utc_iso()
        self._conn.execute(
            "UPDATE achievements SET state = ?, current = ?, target = ?, progress = ?, "
            "unlocked = ?, unlocked_at = ?, updated_at = ? WHERE id = ?",
            (
                json.dumps(row.state, separators=(",", ":")),
                row.current,
                row.target,
                row.progress,
                int(row.unlocked),
                row.unlocked_at,
                row.updated_at,
                row.id,
            ),
        )
        self._conn.commit()
        return row

    def get_achievement(self, achievement_id: str) -> AchievementRow | None:
        row = self._conn.execute(
            "SELECT * FROM achievements WHERE id = ?", (achievement_id,)
        ).fetchone()
        return self._row_to_achievement(row) if row is not None else None

    # --- activity feed --------------------------------------------------------------
    def append_feed(self, events: list[dict[str, Any]]) -> int:
        """Mirror pulled server events for the offline activity list."""
        inserted = 0
        for event in events:
            cursor = self._conn.execute(
                "INSERT OR IGNORE INTO feed (id, event_type, payload, occurred_at, received_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    str(event.get("id", "")),
                    str(event.get("event_type", "")),
                    json.dumps(event.get("payload", {}), separators=(",", ":")),
                    str(event.get("occurred_at", utc_iso())),
                    str(event.get("received_at", utc_iso())),
                ),
            )
            inserted += cursor.rowcount
        if inserted:
            self._conn.execute(
                "DELETE FROM feed WHERE id NOT IN ("
                "  SELECT id FROM feed ORDER BY occurred_at DESC LIMIT 500)"
            )
            self._conn.commit()
        return inserted

    def feed(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM feed ORDER BY occurred_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]
