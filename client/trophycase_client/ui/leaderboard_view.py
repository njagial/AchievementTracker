"""Leaderboard screen."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import flet as ft

from trophycase_client.ui.components import empty_state

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trophycase_client.app import AppShell

__all__ = ["LeaderboardView"]


class LeaderboardView:
    """Global and friends rankings."""

    def __init__(self, shell: AppShell) -> None:
        self._shell = shell
        self._empty = empty_state(
            ft.Icons.LEADERBOARD,
            "Nothing here yet",
            "Unlock achievements to climb the ranking.",
            shell.palette,
        )
        self._list = ft.ListView(controls=[], spacing=8, expand=True)
        self._global_button = ft.Button(
            content=ft.Text("Global"),
            bgcolor=shell.palette.accent,
            color=ft.Colors.WHITE,
            on_click=lambda _e: self._switch("global"),
        )
        self._friends_button = ft.Button(
            content=ft.Text("Friends"),
            on_click=lambda _e: self._switch("friends"),
        )
        self.control = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Text(
                        "Leaderboard",
                        size=24,
                        weight=ft.FontWeight.BOLD,
                        color=shell.palette.text,
                    ),
                    ft.Row(
                        controls=[self._global_button, self._friends_button],
                        spacing=10,
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

    def _switch(self, scope: str) -> None:
        selected = self._shell.state.leaderboard_scope
        if scope == selected:
            return
        self._global_button.bgcolor = self._shell.palette.accent if scope == "global" else None
        self._global_button.color = ft.Colors.WHITE if scope == "global" else None
        self._friends_button.bgcolor = self._shell.palette.accent if scope == "friends" else None
        self._friends_button.color = ft.Colors.WHITE if scope == "friends" else None
        self._shell.run(self._shell.state.load_leaderboard(scope))

    def refresh(self) -> None:
        entries: list[dict[str, Any]] = self._shell.state.leaderboard
        palette = self._shell.palette
        rows: list[ft.Control] = []
        for entry in entries:
            you = bool(entry.get("you"))
            rows.append(
                ft.Container(
                    content=ft.Row(
                        controls=[
                            ft.Text(
                                f"#{int(entry.get('rank', 0))}",
                                size=14,
                                weight=ft.FontWeight.BOLD,
                                color=palette.muted,
                                width=44,
                            ),
                            ft.CircleAvatar(
                                content=ft.Text(
                                    str(entry.get("display_name", "?"))[:1].upper(),
                                    size=14,
                                    color=ft.Colors.WHITE,
                                ),
                                bgcolor=palette.accent if you else palette.border,
                                radius=16,
                            ),
                            ft.Column(
                                controls=[
                                    ft.Text(
                                        str(entry.get("display_name", "someone")),
                                        size=14,
                                        weight=ft.FontWeight.W_600 if you else ft.FontWeight.NORMAL,
                                        color=palette.text,
                                    ),
                                    ft.Text(
                                        "you" if you else " ",
                                        size=11,
                                        color=palette.accent,
                                    ),
                                ],
                                spacing=0,
                                expand=True,
                            ),
                            ft.Text(
                                f"{int(entry.get('score', 0))} pts",
                                size=14,
                                weight=ft.FontWeight.BOLD,
                                color=palette.success if you else palette.text,
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        spacing=10,
                    ),
                    padding=12,
                    bgcolor=palette.card,
                    border_radius=14,
                    border=ft.Border.all(1, palette.accent if you else palette.border),
                )
            )
        self._list.controls = rows
        self._empty.visible = not rows
        scope = self._shell.state.leaderboard_scope
        self._global_button.bgcolor = palette.accent if scope == "global" else None
        self._global_button.color = ft.Colors.WHITE if scope == "global" else None
        self._friends_button.bgcolor = palette.accent if scope == "friends" else None
        self._friends_button.color = ft.Colors.WHITE if scope == "friends" else None
