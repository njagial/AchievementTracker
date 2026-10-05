"""Activity feed: what happened, including everything replayed from the outbox."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import flet as ft

from trophycase_client.ui.components import activity_tile, empty_state

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trophycase_client.app import AppShell

__all__ = ["ActivityView"]


class ActivityView:
    """Recent events pulled from the server (plus locally queued ones)."""

    def __init__(self, shell: AppShell) -> None:
        self._shell = shell
        self._empty = empty_state(
            ft.Icons.EVENT_AVAILABLE,
            "No activity yet",
            "Events you log appear here, online or offline.",
            shell.palette,
        )
        self._list = ft.ListView(controls=[], spacing=4, expand=True)
        self.control = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    ft.Text(
                                        "Activity",
                                        size=24,
                                        weight=ft.FontWeight.BOLD,
                                        color=shell.palette.text,
                                    ),
                                    ft.Text(
                                        "Pulled behind your sync cursor.",
                                        size=13,
                                        color=shell.palette.muted,
                                    ),
                                ],
                                spacing=2,
                                expand=True,
                            ),
                            ft.IconButton(
                                icon=ft.Icons.REFRESH,
                                tooltip="Sync now",
                                on_click=lambda _e: shell.run(shell.state.refresh()),
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self._empty,
                    self._list,
                ],
                spacing=14,
                expand=True,
            ),
            padding=ft.Padding.only(left=16, right=16, top=8, bottom=8),
            expand=True,
        )

    def refresh(self) -> None:
        events: list[dict[str, Any]] = self._shell.state.feed
        palette = self._shell.palette
        self._list.controls = [activity_tile(event, palette) for event in events]
        self._empty.visible = not events
