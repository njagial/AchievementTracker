"""Application state: the single source of truth every view renders from.

``AppState`` owns the offline store, the API client and the sync engine, and exposes
plain data plus observers. Views never talk to the network directly - they call an
operation here and re-render when :meth:`notify` fires.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from trophycase_client.api import ApiClient, ApiError
from trophycase_client.config import ClientConfig
from trophycase_client.offline.store import AchievementRow, LocalStore
from trophycase_client.offline.sync import LocalEvent, SyncEngine, SyncReport

__all__ = ["AppState", "Toast"]

Observer = Callable[[], None]


class Toast:
    """An unlock (or error) announcement the UI shows as a snackbar."""

    __slots__ = ("kind", "message", "title")

    def __init__(self, kind: str, title: str, message: str) -> None:
        self.kind = kind
        self.title = title
        self.message = message


class AppState:
    """Offline-first state container."""

    def __init__(self, config: ClientConfig, store: LocalStore | None = None) -> None:
        self.config = config
        self.store = store if store is not None else LocalStore(config.database_path)
        self.api = ApiClient(config, self.store)
        self.sync = SyncEngine(self.store, self.api)

        self.user: dict[str, Any] | None = _load_user(self.store)
        self.online = False
        self.last_error: str | None = None
        self.busy = False
        self.leaderboard_scope = "global"
        self.leaderboard: list[dict[str, Any]] = []
        self.feed: list[dict[str, Any]] = self.store.feed()
        self.achievements: list[AchievementRow] = self.store.list_achievements()
        self.toasts: list[Toast] = []
        self._observers: list[Observer] = []

    # --- observers -----------------------------------------------------------------
    def subscribe(self, observer: Observer) -> Callable[[], None]:
        self._observers.append(observer)

        def unsubscribe() -> None:
            if observer in self._observers:
                self._observers.remove(observer)

        return unsubscribe

    def notify(self) -> None:
        for observer in list(self._observers):
            observer()

    def push_toast(self, kind: str, title: str, message: str) -> None:
        self.toasts.append(Toast(kind, title, message))

    def pop_toast(self) -> Toast | None:
        return self.toasts.pop(0) if self.toasts else None

    # --- session -------------------------------------------------------------------
    @property
    def is_signed_in(self) -> bool:
        return self.api.is_authenticated

    @property
    def display_name(self) -> str:
        if self.user:
            return str(self.user.get("display_name") or self.user.get("email") or "you")
        return "you"

    async def sign_in(self, email: str, password: str) -> None:
        payload = await self.api.login(email=email, password=password)
        self.user = payload.get("user") if isinstance(payload.get("user"), dict) else None
        self.last_error = None
        await self.refresh()

    async def sign_up(self, email: str, password: str, display_name: str) -> None:
        payload = await self.api.register(email=email, password=password, display_name=display_name)
        self.user = payload.get("user") if isinstance(payload.get("user"), dict) else None
        self.last_error = None
        await self.refresh()

    async def sign_out(self) -> None:
        await self.api.logout()
        self.user = None
        self.online = False
        self.notify()

    # --- data operations -----------------------------------------------------------
    async def refresh(self) -> SyncReport:
        """Push the outbox, reconcile the catalogue and reload derived views."""
        self.busy = True
        self.notify()
        try:
            report = await self.sync.flush()
        finally:
            self.busy = False
        self.achievements = self.store.list_achievements()
        self.feed = self.store.feed()
        self.online = report.online
        self.last_error = report.error
        if report.ok:
            await self._reload_leaderboard()
        self.notify()
        return report

    def log_event(self, event_type: str, payload: dict[str, Any] | None = None) -> LocalEvent:
        """Optimistically record an activity: instant UI, queued for the network."""
        recorded = self.sync.record_event(
            event_type=event_type, payload=payload, occurred_at=datetime.now(UTC)
        )
        self.achievements = self.store.list_achievements()
        for unlocked in recorded.unlocked:
            self.push_toast(
                "unlock",
                f"{unlocked.title} unlocked",
                f"+{unlocked.points} points \u2014 logged offline, syncing when you reconnect",
            )
        self.notify()
        return recorded

    async def create_achievement(
        self,
        *,
        slug: str,
        title: str,
        description: str,
        icon: str,
        points: int,
        rule: dict[str, Any],
    ) -> None:
        try:
            await self.api.create_achievement(
                slug=slug,
                title=title,
                description=description,
                icon=icon,
                points=points,
                rule=rule,
            )
        except ApiError as exc:
            self.last_error = exc.message
            self.push_toast("error", "Could not save achievement", exc.message)
            self.notify()
            return
        self.last_error = None
        await self.refresh()

    async def load_leaderboard(self, scope: str) -> None:
        self.leaderboard_scope = scope
        await self._reload_leaderboard()
        self.notify()

    async def _reload_leaderboard(self) -> None:
        try:
            payload = await self.api.leaderboard(self.leaderboard_scope)
        except ApiError:
            return
        entries = payload.get("entries")
        if isinstance(entries, list):
            self.leaderboard = [entry for entry in entries if isinstance(entry, dict)]

    # --- realtime ------------------------------------------------------------------
    async def handle_message(self, message: dict[str, Any]) -> None:
        """Fold a WebSocket push into local state and wake the UI."""
        kind = str(message.get("type", ""))
        if kind == "progress.updated":
            self.store.update_progress(
                str(message.get("achievement_id", "")),
                current=float(message.get("current", 0.0)),
                target=float(message.get("target", 1.0)),
                progress=float(message.get("progress", 0.0)),
                completed=bool(message.get("completed", False)),
            )
        elif kind == "achievement.unlocked":
            self.store.update_progress(
                str(message.get("achievement_id", "")),
                current=float(message.get("current", 1.0)),
                target=float(message.get("target", 1.0)),
                progress=1.0,
                completed=True,
                unlocked_at=str(message.get("unlocked_at") or ""),
            )
            self.push_toast(
                "unlock",
                f"{message.get('title', 'Achievement')} unlocked",
                str(message.get("description", "")) or f"+{message.get('points', 0)} points",
            )
        elif kind == "leaderboard.changed":
            await self._reload_leaderboard()
        elif kind == "connected":
            self.online = True
        else:
            return  # heartbeat / pong / ack need no rendering
        self.achievements = self.store.list_achievements()
        self.notify()

    # --- lifecycle -----------------------------------------------------------------
    async def close(self) -> None:
        self.store.close()
        await self.api.aclose()


def _load_user(store: LocalStore) -> dict[str, Any] | None:
    raw = store.get_meta("user")
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None
