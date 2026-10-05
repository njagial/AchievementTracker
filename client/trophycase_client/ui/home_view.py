"""Home: the achievement catalogue with one-tap logging and offline badges."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import flet as ft

from trophycase_client.offline.store import AchievementRow
from trophycase_client.ui.components import AchievementCard, empty_state, section_title

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trophycase_client.app import AppShell

__all__ = ["HomeView"]


class HomeView:
    """Catalogue of achievements plus the 'new achievement' dialog."""

    def __init__(self, shell: AppShell) -> None:
        self._shell = shell
        self._cards: dict[str, AchievementCard] = {}
        self._empty = empty_state(
            ft.Icons.EMOJI_EVENTS,
            "No achievements yet",
            "Create your first achievement, then tap Log on a card to make progress.",
            shell.palette,
        )
        self._list = ft.ListView(controls=[], spacing=0, expand=True)
        self._summary = ft.Text("", size=12, color=shell.palette.muted)
        self._header = ft.Row(
            controls=[
                section_title("ACHIEVEMENTS", shell.palette),
                ft.Container(expand=True),
                self._summary,
            ],
            vertical_alignment=ft.CrossAxisAlignment.CENTER,
        )
        self.control = ft.Container(
            content=ft.Column(
                controls=[
                    ft.Row(
                        controls=[
                            ft.Column(
                                controls=[
                                    ft.Text(
                                        "Your trophies",
                                        size=24,
                                        weight=ft.FontWeight.BOLD,
                                        color=shell.palette.text,
                                    ),
                                    ft.Text(
                                        "Tap Log to record progress \u2014 it queues offline.",
                                        size=13,
                                        color=shell.palette.muted,
                                    ),
                                ],
                                spacing=2,
                                expand=True,
                            ),
                            ft.Button(
                                icon=ft.Icons.ADD,
                                content=ft.Text("New"),
                                bgcolor=shell.palette.accent,
                                color=ft.Colors.WHITE,
                                on_click=lambda _e: self._open_creator(),
                                tooltip="Create an achievement",
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                    self._header,
                    self._empty,
                    self._list,
                ],
                spacing=14,
                expand=True,
            ),
            padding=ft.Padding.only(left=16, right=16, top=8, bottom=8),
            expand=True,
        )

    # --- rendering -----------------------------------------------------------------
    def refresh(self) -> None:
        palette = self._shell.palette
        rows = self._shell.state.achievements
        unlocked = sum(1 for row in rows if row.unlocked)
        score = sum(row.points for row in rows if row.unlocked)
        self._summary.value = f"{unlocked}/{len(rows)} unlocked  \u00b7  {score} pts"

        seen: set[str] = set()
        controls: list[ft.Control] = []
        for row in rows:
            seen.add(row.id)
            card = self._cards.get(row.id)
            if card is None:
                card = AchievementCard(row, palette, on_log=self._on_log)
                self._cards[row.id] = card
            else:
                card.update(row)
            controls.append(card.control)
        for missing in set(self._cards) - seen:
            del self._cards[missing]
        self._list.controls = controls
        self._empty.visible = not rows
        self._header.visible = bool(rows)

    def _on_log(self, row: AchievementRow) -> None:
        event_type = str((row.rule or {}).get("event_type", "activity"))
        payload: dict[str, Any] = {"source": "client"}
        value_field = (row.rule or {}).get("value_field")
        if isinstance(value_field, str) and value_field:
            payload[value_field] = 1
        target_id = (row.rule or {}).get("target_id")
        if isinstance(target_id, str) and target_id:
            payload["target_id"] = target_id
        self._shell.log_activity(event_type, payload)

    # --- creation ------------------------------------------------------------------
    def _build_creator(self) -> ft.AlertDialog:
        shell = self._shell
        palette = shell.palette
        name = ft.TextField(label="Title", expand=True)
        description = ft.TextField(label="Description", multiline=True, min_lines=1, max_lines=3)
        slug = ft.TextField(label="Slug (a-z, 0-9, -)", value="")
        icon = ft.TextField(label="Icon", value="trophy")
        points = ft.TextField(label="Points", value="10", keyboard_type=ft.KeyboardType.NUMBER)
        kind = ft.Dropdown(
            label="Rule",
            value="counter",
            options=[
                ft.DropdownOption(key="counter", text="Count N events"),
                ft.DropdownOption(key="boolean", text="First matching event"),
                ft.DropdownOption(key="milestone", text="Sum a value to a threshold"),
                ft.DropdownOption(key="streak", text="Consecutive-day streak"),
            ],
        )
        event_type = ft.TextField(label="Event type", value="task.completed")
        count = ft.TextField(label="N / days / threshold", value="5")
        error = ft.Text("", size=12, color=palette.danger, visible=False)

        def build_rule() -> dict[str, Any]:
            rule: dict[str, Any] = {
                "kind": kind.value or "counter",
                "event_type": event_type.value or "task.completed",
            }
            numeric = _to_float(count.value)
            if rule["kind"] == "counter":
                rule["count"] = int(numeric or 1)
            elif rule["kind"] == "streak":
                rule["days"] = int(numeric or 2)
            elif rule["kind"] == "milestone":
                rule["threshold"] = numeric or 10
                rule["value_field"] = "value"
            return rule

        def close(_e: Any = None) -> None:
            shell.page.pop_dialog()

        async def save(_e: Any) -> None:
            title = (name.value or "").strip()
            if not title:
                error.value = "Title is required."
                error.visible = True
                shell.refresh_chrome()
                return
            final_slug = (slug.value or "").strip() or _slugify(title)
            await shell.state.create_achievement(
                slug=final_slug,
                title=title,
                description=(description.value or "").strip(),
                icon=(icon.value or "trophy").strip(),
                points=_to_int(points.value, 10),
                rule=build_rule(),
            )
            close()

        dialog = ft.AlertDialog(
            title=ft.Text("New achievement", color=palette.text),
            content=ft.Column(
                controls=[
                    name,
                    description,
                    ft.Row(controls=[slug, icon], spacing=10),
                    ft.Row(controls=[points, count], spacing=10),
                    kind,
                    event_type,
                    error,
                ],
                spacing=12,
                tight=True,
                width=420,
            ),
            actions=[
                ft.Button(content=ft.Text("Cancel"), on_click=close),
                ft.Button(
                    content=ft.Text("Create"),
                    bgcolor=palette.accent,
                    color=ft.Colors.WHITE,
                    on_click=lambda e: shell.run(save(e)),
                ),
            ],
            bgcolor=palette.card,
        )
        return dialog

    def _open_creator(self) -> None:
        self._shell.page.show_dialog(self._build_creator())

    def rebuild(self) -> None:
        """Palette changed: drop cached cards so they rebuild with new colours."""
        self._cards.clear()


def _slugify(value: str) -> str:
    keep = [ch.lower() if ch.isalnum() else "-" for ch in value.strip()]
    slug = "".join(keep)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-")[:64] or "achievement"


def _to_float(raw: str | None) -> float | None:
    try:
        return float((raw or "").strip()) if (raw or "").strip() else None
    except ValueError:
        return None


def _to_int(raw: str | None, default: int) -> int:
    value = _to_float(raw)
    return int(value) if value is not None else default
