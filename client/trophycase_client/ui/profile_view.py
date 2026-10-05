"""Profile: account details, sync status, appearance and sign-out."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import flet as ft

from trophycase_client.ui.components import stat_chip

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trophycase_client.app import AppShell

__all__ = ["ProfileView"]


class ProfileView:
    def __init__(self, shell: AppShell) -> None:
        self._shell = shell
        state = shell.state
        user = state.user or {}
        self._name = ft.Text(
            str(user.get("display_name") or "Signed in"),
            size=24,
            weight=ft.FontWeight.BOLD,
            color=shell.palette.text,
        )
        self._email = ft.Text(str(user.get("email") or ""), size=13, color=shell.palette.muted)
        self._status = ft.Row(
            controls=[
                ft.Icon(ft.Icons.CLOUD_DONE, size=16, color=shell.palette.success),
                ft.Text("Online", size=12, color=shell.palette.muted),
            ],
            spacing=6,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self._unlocked_chip = stat_chip("unlocked", "0", shell.palette)
        self._score_chip = stat_chip("points", "0", shell.palette)
        self._queued_chip = stat_chip("queued", "0", shell.palette)
        self._theme_switch = ft.Switch(
            label="Dark mode",
            value=shell.is_dark,
            on_change=self._toggle_theme,
        )
        self._sync_button = ft.Button(
            content=ft.Text("Sync now"),
            icon=ft.Icons.SYNC,
            bgcolor=shell.palette.accent,
            color=ft.Colors.WHITE,
            on_click=lambda _e: shell.run(shell.state.refresh()),
        )
        self._sign_out = ft.Button(
            content=ft.Text("Sign out"),
            icon=ft.Icons.LOGOUT,
            style=ft.ButtonStyle(color=shell.palette.danger),
            on_click=lambda _e: shell.run(self._sign_out_now()),
        )
        self.control = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Container(
                        content=ft.Row(
                            controls=[
                                ft.CircleAvatar(
                                    content=ft.Text(
                                        (str(user.get("display_name") or "?"))[:1].upper(),
                                        size=22,
                                        color=ft.Colors.WHITE,
                                    ),
                                    bgcolor=shell.palette.accent,
                                    radius=26,
                                ),
                                ft.Column(
                                    controls=[self._name, self._email],
                                    spacing=2,
                                    expand=True,
                                ),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=14,
                        ),
                        padding=18,
                        bgcolor=shell.palette.card,
                        border_radius=16,
                        border=ft.Border.all(1, shell.palette.border),
                    ),
                    ft.Row(
                        controls=[self._unlocked_chip, self._score_chip, self._queued_chip],
                        spacing=10,
                    ),
                    ft.Container(
                        content=ft.Column(
                            controls=[
                                self._status,
                                ft.Divider(color=shell.palette.border),
                                self._theme_switch,
                                ft.Divider(color=shell.palette.border),
                                self._sync_button,
                                self._sign_out,
                            ],
                            spacing=14,
                        ),
                        padding=18,
                        bgcolor=shell.palette.card,
                        border_radius=16,
                        border=ft.Border.all(1, shell.palette.border),
                    ),
                ],
                spacing=16,
                expand=True,
            ),
            padding=ft.Padding.only(left=16, right=16, top=8, bottom=8),
            expand=True,
        )

    async def _sign_out_now(self) -> None:
        await self._shell.state.sign_out()

    def _toggle_theme(self, _event: Any = None) -> None:
        self._shell.set_dark(bool(self._theme_switch.value))

    def refresh(self) -> None:
        state = self._shell.state
        user = state.user or {}
        self._name.value = str(user.get("display_name") or "Signed in")
        self._email.value = str(user.get("email") or "")
        unlocked = sum(1 for row in state.achievements if row.unlocked)
        score = sum(row.points for row in state.achievements if row.unlocked)
        _set_chip(self._unlocked_chip, str(unlocked))
        _set_chip(self._score_chip, str(score))
        _set_chip(self._queued_chip, str(state.store.pending_count()))
        self._status.controls = [
            ft.Icon(
                ft.Icons.CLOUD_DONE if state.online else ft.Icons.CLOUD_OFF,
                size=16,
                color=self._shell.palette.success if state.online else self._shell.palette.muted,
            ),
            ft.Text(
                "Online" if state.online else "Offline \u2014 changes are queued",
                size=12,
                color=self._shell.palette.muted,
            ),
        ]
        self._theme_switch.value = self._shell.is_dark


def _set_chip(container: ft.Container, value: str) -> None:
    column = container.content
    if isinstance(column, ft.Column) and column.controls:
        label = column.controls[0]
        if isinstance(label, ft.Text):
            label.value = value
