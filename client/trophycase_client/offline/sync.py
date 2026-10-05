"""Offline-first sync engine: queue, push, reconcile, pull.

Ordering is deliberate:

1. **Push** everything in the outbox (idempotent on ``op_id``).
2. **Reconcile** the cached catalogue with the server's authoritative snapshot. Local
   optimistic progress is merged rather than thrown away, because the worker may not
   have evaluated the batch yet - but if the server *rejected* an operation the server
   view wins outright, otherwise drift would never heal.
3. **Pull** the event feed behind the stored cursor for the offline activity list.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from trophycase_client.api import ApiClient, ApiError, AuthError
from trophycase_client.offline.store import AchievementRow, LocalStore, utc_iso

__all__ = ["LocalEvent", "SyncEngine", "SyncReport"]


@dataclass(frozen=True, slots=True)
class LocalEvent:
    """Result of recording an event while offline (or before the round-trip)."""

    op_id: str
    changed: list[AchievementRow]
    unlocked: list[AchievementRow]


@dataclass(frozen=True, slots=True)
class SyncReport:
    pushed: int = 0
    applied: int = 0
    duplicates: int = 0
    conflicts: int = 0
    rejected: int = 0
    pulled: int = 0
    refreshed: bool = False
    online: bool = True
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class SyncEngine:
    """Drives the outbox and the local mirror against the API."""

    def __init__(self, store: LocalStore, api: ApiClient) -> None:
        self._store = store
        self._api = api

    @property
    def pending_count(self) -> int:
        return self._store.pending_count()

    # --- local writes ---------------------------------------------------------------
    def record_event(
        self,
        *,
        event_type: str,
        payload: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> LocalEvent:
        """Persist an event offline, apply it optimistically, and queue it for push."""
        body = payload or {}
        when = occurred_at or datetime.now(UTC)
        op_id = self._store.queue_event(event_type=event_type, payload=body, occurred_at=when)
        changed = self._store.apply_local_event(
            event_type=event_type, payload=body, occurred_at=when
        )
        return LocalEvent(
            op_id=op_id,
            changed=changed,
            unlocked=[row for row in changed if row.unlocked],
        )

    def queue_profile(self, fields: dict[str, Any], base_updated_at: str | None) -> None:
        self._store.queue_profile(fields, base_updated_at)
        self._store.drop_empty_profiles()

    # --- round trip ------------------------------------------------------------------
    async def flush(self) -> SyncReport:
        """Push, reconcile and pull. Never raises for connectivity problems."""
        self._store.drop_empty_profiles()
        pushed = applied = duplicates = conflicts = rejected = 0
        ops = self._store.pending()
        if ops:
            try:
                payload = await self._api.push_ops([op.to_wire() for op in ops])
            except AuthError as exc:
                return SyncReport(pushed=len(ops), online=False, error=exc.message)
            except ApiError as exc:
                for op in ops:
                    self._store.mark_failed(op.op_id, exc.message)
                return SyncReport(pushed=len(ops), online=False, error=exc.message)
            results = {
                str(item["op_id"]): item for item in payload.get("results", []) if "op_id" in item
            }
            finished: list[str] = []
            for op in ops:
                result = results.get(op.op_id)
                if result is None:
                    continue  # server did not answer for this op: keep it queued
                status = str(result.get("status", ""))
                if result.get("conflict") is True:
                    conflicts += 1
                if status == "applied":
                    applied += 1
                elif status == "duplicate":
                    duplicates += 1
                elif status == "rejected":
                    rejected += 1
                else:
                    self._store.mark_failed(op.op_id, f"unknown status {status!r}")
                    continue
                finished.append(op.op_id)
            self._store.delete_ops(finished)
            pushed = len(ops)

        refreshed = False
        try:
            catalogue = await self._api.catalogue()
            rows = _rows_from_server(catalogue)
            merged = _merge(self._store.list_achievements(), rows, authoritative=rejected > 0)
            self._store.replace_catalogue(merged)
            refreshed = True
        except AuthError as exc:
            return SyncReport(
                pushed=pushed,
                applied=applied,
                duplicates=duplicates,
                conflicts=conflicts,
                rejected=rejected,
                online=False,
                error=exc.message,
            )
        except ApiError as exc:
            # Offline reads still work: the cached catalogue is shown instead.
            return SyncReport(
                pushed=pushed,
                applied=applied,
                duplicates=duplicates,
                conflicts=conflicts,
                rejected=rejected,
                online=False,
                error=exc.message,
            )

        pulled = 0
        cursor = self._store.get_meta("cursor")
        try:
            changes = await self._api.pull_changes(cursor)
        except ApiError:
            changes = {}
        events = changes.get("events") if isinstance(changes, dict) else None
        if isinstance(events, list):
            pulled = self._store.append_feed(events)
            new_cursor = changes.get("cursor")
            if isinstance(new_cursor, str) and new_cursor:
                self._store.set_meta("cursor", new_cursor)

        return SyncReport(
            pushed=pushed,
            applied=applied,
            duplicates=duplicates,
            conflicts=conflicts,
            rejected=rejected,
            pulled=pulled,
            refreshed=refreshed,
            online=True,
        )

    async def prime(self) -> SyncReport:
        """First load after sign-in: catalogue + feed without touching the outbox."""
        return await self.flush()


def _rows_from_server(payload: dict[str, Any]) -> list[AchievementRow]:
    rows: list[AchievementRow] = []
    for item in payload.get("achievements", []):
        state = item.get("state") if isinstance(item.get("state"), dict) else {}
        rows.append(
            AchievementRow(
                id=str(item["id"]),
                slug=str(item["slug"]),
                title=str(item["title"]),
                description=str(item.get("description", "")),
                icon=str(item.get("icon", "trophy")),
                points=int(item.get("points", 0)),
                rule=item.get("rule") if isinstance(item.get("rule"), dict) else {},
                state=state if isinstance(state, dict) else {},
                current=float(item.get("current", 0.0)),
                target=float(item.get("target", 1.0)),
                progress=float(item.get("progress", 0.0)),
                unlocked=bool(item.get("unlocked", False)),
                unlocked_at=item.get("unlocked_at"),
                updated_at=utc_iso(),
            )
        )
    return rows


def _merge(
    local: list[AchievementRow], server: list[AchievementRow], *, authoritative: bool
) -> list[AchievementRow]:
    """Combine the optimistic local mirror with the server snapshot."""
    if authoritative:
        return server
    by_id = {row.id: row for row in local}
    merged: list[AchievementRow] = []
    for row in server:
        cached = by_id.get(row.id)
        if cached is None:
            merged.append(row)
            continue
        pending_ahead = cached.current > row.current or (cached.unlocked and not row.unlocked)
        if not pending_ahead:
            merged.append(row)  # server caught up: drop the optimistic guess
            continue
        merged.append(
            AchievementRow(
                id=row.id,
                slug=row.slug,
                title=row.title,
                description=row.description,
                icon=row.icon,
                points=row.points,
                rule=row.rule or cached.rule,
                state=_merge_state(cached.state, row.state),
                current=max(cached.current, row.current),
                target=row.target,
                progress=max(cached.progress, row.progress),
                unlocked=cached.unlocked or row.unlocked,
                unlocked_at=cached.unlocked_at or row.unlocked_at,
                updated_at=utc_iso(),
            )
        )
    return merged


def _merge_state(cached: dict[str, Any], server: dict[str, Any]) -> dict[str, Any]:
    cached_day = _parse_day(cached.get("last_day"))
    server_day = _parse_day(server.get("last_day"))
    latest = (
        max(cached_day, server_day) if cached_day and server_day else (cached_day or server_day)
    )
    return {
        "count": max(int(cached.get("count", 0)), int(server.get("count", 0))),
        "value": max(float(cached.get("value", 0.0)), float(server.get("value", 0.0))),
        "streak": max(int(cached.get("streak", 0)), int(server.get("streak", 0))),
        "last_day": latest.isoformat() if latest else None,
    }


def _parse_day(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
