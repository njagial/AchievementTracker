"""Reusable controls: achievement cards, empty states, chips and toasts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import flet as ft

from trophycase_client.offline.store import AchievementRow, utc_iso
from trophycase_client.ui.theme import Palette

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import Callable

__all__ = [
    "AchievementCard",
    "empty_state",
    "icon_for",
    "section_title",
    "snackbar_for",
    "stat_chip",
]

_ICONS: dict[str, ft.IconData] = {
    "trophy": ft.Icons.EMOJI_EVENTS,
    "star": ft.Icons.STAR,
    "bolt": ft.Icons.BOLT,
    "fire": ft.Icons.LOCAL_FIRE_DEPARTMENT,
    "check": ft.Icons.CHECK_CIRCLE,
    "lock": ft.Icons.LOCK,
    "medal": ft.Icons.MILITARY_TECH,
    "clock": ft.Icons.EVENT_AVAILABLE,
    "group": ft.Icons.GROUP,
}


def icon_for(name: str) -> ft.IconData:
    return _ICONS.get(name, ft.Icons.STAR_BORDER)


def section_title(text: str, palette: Palette) -> ft.Text:
    return ft.Text(text, size=13, weight=ft.FontWeight.W_600, color=palette.muted)


def stat_chip(label: str, value: str, palette: Palette) -> ft.Container:
    return ft.Container(
        content=ft.Column(
            controls=[
                ft.Text(value, size=20, weight=ft.FontWeight.BOLD, color=palette.text),
                ft.Text(label, size=11, color=palette.muted),
            ],
            spacing=0,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        padding=14,
        bgcolor=palette.card,
        border_radius=14,
        border=ft.Border.all(1, palette.border),
        expand=True,
    )


def empty_state(icon: ft.IconData, title: str, message: str, palette: Palette) -> ft.Control:
    return ft.Container(
        content=ft.Column(
            controls=[
                ft.Icon(icon, size=42, color=palette.muted),
                ft.Text(title, size=16, weight=ft.FontWeight.W_600, color=palette.text),
                ft.Text(
                    message,
                    size=13,
                    color=palette.muted,
                    text_align=ft.TextAlign.CENTER,
                ),
            ],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            spacing=8,
        ),
        padding=32,
        alignment=ft.Alignment.CENTER,
    )


def snackbar_for(kind: str, title: str, message: str, palette: Palette) -> ft.SnackBar:
    unlock = kind == "unlock"
    accent = palette.success if unlock else palette.danger
    return ft.SnackBar(
        content=ft.Row(
            controls=[
                ft.Icon(
                    ft.Icons.EMOJI_EVENTS if unlock else ft.Icons.WARNING,
                    color=accent,
                ),
                ft.Column(
                    controls=[
                        ft.Text(title, weight=ft.FontWeight.BOLD, color=ft.Colors.WHITE),
                        ft.Text(message, size=12, color="#E6E9EC"),
                    ],
                    spacing=0,
                    tight=True,
                ),
            ],
            spacing=12,
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        ),
        bgcolor=palette.surface,
        duration=4000,
    )


class AchievementCard:
    """One achievement with live progress; updated in place to keep scroll position."""

    def __init__(
        self,
        row: AchievementRow,
        palette: Palette,
        on_log: Callable[[AchievementRow], None],
    ) -> None:
        self.row = row
        self.palette = palette
        self._on_log = on_log

        self._title = ft.Text(row.title, size=15, weight=ft.FontWeight.W_600, color=palette.text)
        self._description = ft.Text(
            row.description or "No description", size=12, color=palette.muted, max_lines=2
        )
        self._numbers = ft.Text(self._numbers_text(row), size=12, color=palette.muted)
        self._bar = ft.ProgressBar(value=row.progress, color=palette.accent, bar_height=6)
        self._badge = ft.Icon(ft.Icons.CHECK_CIRCLE, size=18, color=palette.success)
        self._points = ft.Text(f"+{row.points}", size=12, weight=ft.FontWeight.BOLD)
        self._log_button = ft.Button(
            content=ft.Text("Log", size=12),
            icon=ft.Icons.ADD,
            height=34,
            width=76,
            bgcolor=palette.accent,
            color=ft.Colors.WHITE,
            disabled=row.unlocked,
            on_click=self._handle_log,
            tooltip=f"Record a {self._event_type(row)} event",
        )

        self.control = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Icon(icon_for(row.icon), color=palette.accent, size=22),
                            ft.Column(
                                controls=[self._title, self._description],
                                spacing=2,
                                expand=True,
                            ),
                            ft.Row(controls=[self._points, self._badge], spacing=6),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.START,
                    ),
                    ft.Row(
                        controls=[self._bar, self._numbers],
                        spacing=10,
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    ft.Row(
                        controls=[self._log_button],
                        alignment=ft.MainAxisAlignment.END,
                    ),
                ],
                spacing=10,
            ),
            padding=14,
            margin=ft.Margin.symmetric(vertical=5),
            bgcolor=palette.card,
            border_radius=16,
            border=ft.Border.all(1, palette.border),
        )

    @staticmethod
    def _event_type(row: AchievementRow) -> str:
        return str((row.rule or {}).get("event_type", "activity"))

    @staticmethod
    def _numbers_text(row: AchievementRow) -> str:
        if row.unlocked:
            return "unlocked"
        target = row.target
        if float(target).is_integer():
            current, goal = int(row.current), int(target)
            return f"{current}/{goal}"
        return f"{row.current:g}/{target:g}"

    def _handle_log(self, _event: Any = None) -> None:
        self._on_log(self.row)

    def update(self, row: AchievementRow) -> None:
        self.row = row
        self._numbers.value = self._numbers_text(row)
        self._bar.value = row.progress
        self._log_button.disabled = row.unlocked
        self._badge.visible = row.unlocked
        if row.unlocked and row.unlocked_at:
            self.control.tooltip = f"Unlocked {row.unlocked_at}"

    @property
    def achievement_id(self) -> str:
        return self.row.id


def activity_tile(event: dict[str, Any], palette: Palette) -> ft.Control:
    when = str(event.get("occurred_at", ""))[:16].replace("T", " ")
    return ft.ListTile(
        leading=ft.Icon(ft.Icons.BOLT_OUTLINED, color=palette.accent),
        title=ft.Text(str(event.get("event_type", "event")), color=palette.text, size=14),
        subtitle=ft.Text(when or utc_iso(), size=12, color=palette.muted),
        dense=True,
    )
