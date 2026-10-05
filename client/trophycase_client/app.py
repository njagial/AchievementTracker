"""Flet application shell: navigation, chrome, background sync and realtime."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable
from concurrent.futures import Future
from typing import Any

import flet as ft

from trophycase_client.config import ClientConfig
from trophycase_client.model import AppState, Toast
from trophycase_client.realtime import RealtimeClient
from trophycase_client.ui.activity_view import ActivityView
from trophycase_client.ui.components import snackbar_for
from trophycase_client.ui.home_view import HomeView
from trophycase_client.ui.leaderboard_view import LeaderboardView
from trophycase_client.ui.login_view import LoginView
from trophycase_client.ui.profile_view import ProfileView
from trophycase_client.ui.theme import Palette, palette_for

logger = logging.getLogger(__name__)

__all__ = ["AppShell", "run"]

_TABS = ("Home", "Activity", "Ranks", "Profile")


class AppShell:
    """Owns the page, the state and every view; observers trigger re-renders."""

    def __init__(self, page: ft.Page, state: AppState) -> None:
        self.page = page
        self.state = state
        self.config: ClientConfig = state.config
        self._mode = ""
        self._index = 0
        self._background: set[Future[Any]] = set()
        self._stopping = False

        self.login_view = LoginView(self)
        self.home_view = HomeView(self)
        self.activity_view = ActivityView(self)
        self.leaderboard_view = LeaderboardView(self)
        self.profile_view = ProfileView(self)
        self._views = (
            self.home_view,
            self.activity_view,
            self.leaderboard_view,
            self.profile_view,
        )
        self._body = ft.Container(expand=True)
        self._status = ft.Row(
            controls=[],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        state.subscribe(self.refresh)

    # --- appearance ----------------------------------------------------------------
    @property
    def palette(self) -> Palette:
        return palette_for(self.page)

    @property
    def is_dark(self) -> bool:
        mode = self.page.theme_mode
        if mode == ft.ThemeMode.DARK:
            return True
        if mode == ft.ThemeMode.LIGHT:
            return False
        return self.page.platform_brightness == ft.Brightness.DARK

    def set_dark(self, dark: bool) -> None:
        self.page.theme_mode = ft.ThemeMode.DARK if dark else ft.ThemeMode.LIGHT
        self.home_view.rebuild()
        self._render(force=True)

    # --- rendering -----------------------------------------------------------------
    def boot(self) -> None:
        """First paint after ``ft.run`` hands us a page."""
        self.page.title = "TrophyCase"
        self.page.theme_mode = ft.ThemeMode.SYSTEM
        self.page.padding = 0
        self.page.on_close = self._on_close
        self._render(force=True)

    def _render(self, *, force: bool = False) -> None:
        mode = "app" if self.state.is_signed_in else "login"
        if not force and mode == self._mode:
            self.refresh()
            return
        previous, self._mode = self._mode, mode
        if mode == "login":
            if previous == "app":
                self._stop_background()
            self.page.clean()
            self.page.add(self.login_view.build())
            self.page.update()
            return

        palette = self.palette
        view = self._views[self._index]
        self.page.clean()
        self.page.add(
            ft.Column(
                controls=[
                    ft.AppBar(
                        title=ft.Row(
                            controls=[
                                ft.Icon(ft.Icons.EMOJI_EVENTS, color=palette.accent),
                                ft.Text("TrophyCase", weight=ft.FontWeight.BOLD),
                            ],
                            spacing=8,
                        ),
                        bgcolor=palette.surface,
                        center_title=False,
                        actions=[self._status],
                    ),
                    self._body,
                    ft.NavigationBar(
                        destinations=[
                            ft.NavigationBarDestination(icon=icon, label=label)
                            for icon, label in zip(
                                (
                                    ft.Icons.EMOJI_EVENTS,
                                    ft.Icons.EVENT_AVAILABLE,
                                    ft.Icons.LEADERBOARD,
                                    ft.Icons.PERSON,
                                ),
                                _TABS,
                                strict=True,
                            )
                        ],
                        selected_index=self._index,
                        on_change=self._on_nav,
                        bgcolor=palette.surface,
                    ),
                ],
                expand=True,
            )
        )
        self._body.content = view.control
        view.refresh()
        self._render_status()
        self.page.update()
        self._ensure_background()

    def _on_nav(self, event: Any) -> None:
        selected = getattr(event, "control", None)
        index = getattr(selected, "selected_index", None)
        self._index = int(index or 0)
        self._render(force=True)

    def _render_status(self) -> None:
        palette = self.palette
        pending = self.state.store.pending_count()
        online = self.state.online
        self._status.controls = [
            ft.Icon(
                ft.Icons.WIFI if online else ft.Icons.WIFI_OFF,
                size=16,
                color=palette.success if online else palette.muted,
            ),
            ft.Row(
                controls=[
                    ft.Icon(
                        ft.Icons.SYNC if pending else ft.Icons.CLOUD_DONE,
                        size=16,
                        color=palette.muted,
                    ),
                    ft.Text(str(pending), size=11, color=palette.muted),
                ],
                spacing=3,
                visible=bool(pending),
            ),
        ]

    def refresh(self) -> None:
        """Observer entry point: repaint chrome, the active view and any toasts."""
        if self._mode == "":
            return
        signed_in = self.state.is_signed_in
        if signed_in and self._mode != "app":
            self._render(force=True)
            return
        if not signed_in and self._mode != "login":
            self._render(force=True)
            return
        if self._mode == "login":
            self.login_view.refresh()
            self.page.update()
            return
        view = self._views[self._index]
        view.refresh()
        self._render_status()
        self._drain_toasts()
        self.page.update()

    def refresh_chrome(self) -> None:
        """Partial repaint for forms (keeps focus in text fields)."""
        self._render_status()
        if self._mode == "login":
            self.page.remove_at(-1)
            self.page.add(self.login_view.build())
        self.page.update()

    def _drain_toasts(self) -> None:
        while True:
            toast = self.state.pop_toast()
            if toast is None:
                return
            self.page.show_dialog(self._snackbar(toast))

    def _snackbar(self, toast: Toast) -> ft.SnackBar:
        return snackbar_for(toast.kind, toast.title, toast.message, self.palette)

    # --- actions -------------------------------------------------------------------
    def run(self, awaitable: Awaitable[Any]) -> None:
        """Run a coroutine on the page loop, logging anything that escapes."""
        self.page.run_task(self._guard, awaitable)

    async def _guard(self, awaitable: Awaitable[Any]) -> None:
        try:
            await awaitable
        except asyncio.CancelledError:  # pragma: no cover - shutdown
            raise
        except Exception:
            logger.exception("background operation failed")

    def log_activity(self, event_type: str, payload: dict[str, Any]) -> None:
        """Optimistic local write followed by a background flush."""
        self.state.log_event(event_type, payload)
        self.run(self.state.refresh())

    # --- background ----------------------------------------------------------------
    def _ensure_background(self) -> None:
        if self._stopping or self._background:
            return
        self._background = {
            self.page.run_task(self._sync_loop),
            self.page.run_task(self._realtime_loop),
        }

    async def _sync_loop(self) -> None:
        interval = self.config.sync_interval_seconds
        while not self._stopping and self.state.is_signed_in:
            await asyncio.sleep(interval)
            with contextlib.suppress(Exception):
                await self.state.refresh()

    async def _realtime_loop(self) -> None:
        client = RealtimeClient(
            self.config,
            token=lambda: self.state.api.access_token,
            on_message=self.state.handle_message,
        )
        try:
            await client.run()
        finally:
            client.stop()

    def _stop_background(self) -> None:
        for task in self._background:
            task.cancel()
        self._background.clear()

    def _on_close(self, _event: Any = None) -> None:
        self._stopping = True
        self._stop_background()
        with contextlib.suppress(Exception):
            self.state.store.close()


def run() -> None:
    """Console entry point (``trophycase-client`` / ``flet run``)."""

    def main(page: ft.Page) -> None:
        shell = AppShell(page, AppState(ClientConfig.load()))
        shell.boot()
        if shell.state.is_signed_in:
            shell._ensure_background()
        page.run_task(shell.state.refresh)

    ft.run(main, name="TrophyCase")
