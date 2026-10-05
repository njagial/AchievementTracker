"""Smoke tests: every view builds and refreshes with a real AppState.

Flet validates control keyword arguments at construction time, so building the views
without a running server is enough to catch API drift (renamed params, dropped enums).
"""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import flet as ft

from trophycase_client.app import AppShell
from trophycase_client.config import ClientConfig
from trophycase_client.model import AppState
from trophycase_client.offline.store import AchievementRow, LocalStore
from trophycase_client.ui.activity_view import ActivityView
from trophycase_client.ui.home_view import HomeView
from trophycase_client.ui.leaderboard_view import LeaderboardView
from trophycase_client.ui.login_view import LoginView
from trophycase_client.ui.profile_view import ProfileView
from trophycase_client.ui.theme import Palette, palette_for

RULE: dict[str, Any] = {"kind": "counter", "event_type": "task.completed", "count": 5}


class _PageStub:
    """Minimal Page stand-in: the theme helper only reads these two attributes."""

    theme_mode = ft.ThemeMode.DARK
    platform_brightness = ft.Brightness.DARK


class FakeShell:
    """Just enough of :class:`AppShell` for the views to render against."""

    def __init__(self, state: AppState, palette: Palette) -> None:
        self.state = state
        self.palette = palette
        self.is_dark = True
        self.page = cast(ft.Page, _PageStub())
        self.ran: list[Any] = []

    def run(self, awaitable: Any) -> None:
        self.ran.append(awaitable)


def make_state(tmp_path: Any) -> AppState:
    config = ClientConfig(
        api_base_url="http://127.0.0.1:9",
        ws_url="ws://127.0.0.1:9/v1/ws",
        device_name="test-device",
        data_dir=tmp_path,
        verify=True,
    )
    state = AppState(config, store=LocalStore(config.database_path))
    state.achievements = [
        AchievementRow(
            id=str(uuid4()),
            slug="first-steps",
            title="First steps",
            description="Complete five tasks",
            icon="trophy",
            points=10,
            rule=RULE,
            state={"count": 2, "value": 0.0, "streak": 0, "last_day": None},
            current=2.0,
            target=5.0,
            progress=0.4,
            unlocked=False,
            unlocked_at=None,
        ),
        AchievementRow(
            id=str(uuid4()),
            slug="unlocked-one",
            title="Warm-up",
            description="Already done",
            icon="star",
            points=5,
            rule=RULE,
            state={"count": 5, "value": 0.0, "streak": 0, "last_day": None},
            current=5.0,
            target=5.0,
            progress=1.0,
            unlocked=True,
            unlocked_at="2026-10-01T10:00:00Z",
        ),
    ]
    state.leaderboard = [
        {"rank": 1, "display_name": "Ada", "score": 120, "you": True, "user_id": "u1"},
        {"rank": 2, "display_name": "Grace", "score": 90, "you": False, "user_id": "u2"},
    ]
    state.feed = [
        {
            "id": "e1",
            "event_type": "task.completed",
            "payload": {},
            "occurred_at": "2026-10-04T08:00:00Z",
            "received_at": "2026-10-04T08:00:01Z",
        }
    ]
    return state


def _shell(tmp_path: Any) -> tuple[FakeShell, AppState]:
    state = make_state(tmp_path)
    shell = FakeShell(state, palette_for(cast(ft.Page, _PageStub())))
    return shell, state


def test_login_view_builds(tmp_path: Any) -> None:
    shell, state = _shell(tmp_path)
    try:
        view = LoginView(cast(AppShell, shell))
        assert isinstance(view.build(), ft.Control)
        view.refresh()
        view.set_error("boom")
        assert view._error.visible
    finally:
        state.store.close()


def test_home_view_renders_and_updates(tmp_path: Any) -> None:
    shell, state = _shell(tmp_path)
    try:
        view = HomeView(cast(AppShell, shell))
        assert isinstance(view.control, ft.Control)
        view.refresh()
        assert len(view._list.controls) == 2
        # A progress change must not recreate cards (scroll position is preserved).
        first_card = view._list.controls[0]
        state.achievements[0].current = 3.0
        view.refresh()
        assert view._list.controls[0] is first_card
    finally:
        state.store.close()


def test_home_view_creator_dialog_builds(tmp_path: Any) -> None:
    shell, state = _shell(tmp_path)
    try:
        view = HomeView(cast(AppShell, shell))
        dialog = view._build_creator()
        assert isinstance(dialog, ft.AlertDialog)
        assert dialog.content is not None
    finally:
        state.store.close()


def test_activity_view_renders(tmp_path: Any) -> None:
    shell, state = _shell(tmp_path)
    try:
        view = ActivityView(cast(AppShell, shell))
        view.refresh()
        assert isinstance(view.control, ft.Control)
        assert view._list.controls
    finally:
        state.store.close()


def test_leaderboard_view_renders(tmp_path: Any) -> None:
    shell, state = _shell(tmp_path)
    try:
        view = LeaderboardView(cast(AppShell, shell))
        view.refresh()
        assert len(view._list.controls) == 2
        assert not view._empty.visible
    finally:
        state.store.close()


def test_profile_view_renders(tmp_path: Any) -> None:
    shell, state = _shell(tmp_path)
    try:
        view = ProfileView(cast(AppShell, shell))
        view.refresh()
        assert isinstance(view.control, ft.Control)
        assert view._queued_chip.content is not None
    finally:
        state.store.close()
