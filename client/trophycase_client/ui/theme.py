"""Colour palettes for the adaptive dark/light UI."""

from __future__ import annotations

from dataclasses import dataclass

import flet as ft

__all__ = ["Palette", "palette_for"]


@dataclass(frozen=True, slots=True)
class Palette:
    """The handful of colours every control draws from."""

    background: str
    surface: str
    card: str
    text: str
    muted: str
    accent: str
    success: str
    danger: str
    border: str


_DARK = Palette(
    background="#101418",
    surface="#171C22",
    card="#1E242B",
    text="#F2F5F7",
    muted="#9AA6B2",
    accent=ft.Colors.BLUE,
    success=ft.Colors.GREEN,
    danger=ft.Colors.RED,
    border="#2A323B",
)

_LIGHT = Palette(
    background="#F6F8FA",
    surface="#FFFFFF",
    card="#FFFFFF",
    text="#101418",
    muted="#5B6672",
    accent=ft.Colors.BLUE,
    success=ft.Colors.GREEN_700 if hasattr(ft.Colors, "GREEN_700") else ft.Colors.GREEN,
    danger=ft.Colors.RED_700 if hasattr(ft.Colors, "RED_700") else ft.Colors.RED,
    border="#E3E8EE",
)


def palette_for(page: ft.Page) -> Palette:
    """Dark palette when the page (or the OS) is dark, light otherwise."""
    mode = page.theme_mode
    dark = mode == ft.ThemeMode.DARK
    if mode == ft.ThemeMode.SYSTEM or mode is None:
        dark = page.platform_brightness == ft.Brightness.DARK
    return _DARK if dark else _LIGHT
