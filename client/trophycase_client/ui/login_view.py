"""Sign-in / sign-up screen."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import flet as ft

from trophycase_client.api import ApiError, AuthError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from trophycase_client.app import AppShell

__all__ = ["LoginView"]


class LoginView:
    """Email + password form with a sign-in / create-account toggle."""

    def __init__(self, shell: AppShell) -> None:
        self._shell = shell
        self._signing_up = False

        self._email = ft.TextField(
            label="Email",
            keyboard_type=ft.KeyboardType.EMAIL,
            expand=True,
        )
        self._password = ft.TextField(
            label="Password",
            password=True,
            can_reveal_password=True,
            on_submit=self._on_submit,
            expand=True,
        )
        self._name = ft.TextField(label="Display name", visible=False, expand=True)
        self._error = ft.Text("", size=13, color=ft.Colors.RED, visible=False)
        self._submit = ft.Button(
            content=ft.Text("Sign in"),
            bgcolor=ft.Colors.BLUE,
            color=ft.Colors.WHITE,
            height=44,
            expand=True,
            on_click=self._on_submit,
        )
        self._toggle = ft.Button(
            content=ft.Text("New here? Create an account"),
            style=ft.ButtonStyle(color=ft.Colors.BLUE),
            on_click=self._on_toggle,
        )

    def build(self) -> ft.Control:
        palette = self._shell.palette
        return ft.Container(
            content=ft.Column(
                controls=[
                    ft.Container(
                        content=ft.Column(
                            controls=[
                                ft.Icon(ft.Icons.EMOJI_EVENTS, size=54, color=palette.accent),
                                ft.Text(
                                    "TrophyCase",
                                    size=30,
                                    weight=ft.FontWeight.BOLD,
                                    color=palette.text,
                                ),
                                ft.Text(
                                    "Offline-first achievements, everywhere you are.",
                                    size=14,
                                    color=palette.muted,
                                    text_align=ft.TextAlign.CENTER,
                                ),
                            ],
                            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                            spacing=6,
                        ),
                        padding=ft.Padding.only(top=8, bottom=24),
                    ),
                    ft.Container(
                        content=ft.Column(
                            controls=[
                                self._email,
                                self._password,
                                self._name,
                                self._error,
                                self._submit,
                                self._toggle,
                            ],
                            spacing=14,
                        ),
                        padding=18,
                        bgcolor=palette.card,
                        border_radius=18,
                        border=ft.Border.all(1, palette.border),
                    ),
                    ft.Text(
                        "Everything works offline; changes sync when you reconnect.",
                        size=12,
                        color=palette.muted,
                        text_align=ft.TextAlign.CENTER,
                    ),
                ],
                horizontal_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=12,
                tight=True,
            ),
            alignment=ft.Alignment.CENTER,
            padding=24,
            expand=True,
        )

    # --- events -------------------------------------------------------------------
    def _on_toggle(self, _event: Any = None) -> None:
        self._signing_up = not self._signing_up
        self._name.visible = self._signing_up
        self._submit.content = ft.Text("Create account") if self._signing_up else ft.Text("Sign in")
        self._toggle.content = (
            ft.Text("Have an account? Sign in")
            if self._signing_up
            else ft.Text("New here? Create an account")
        )
        self.set_error("")
        self._shell.refresh_chrome()

    def _on_submit(self, _event: Any = None) -> None:
        email = (self._email.value or "").strip()
        password = self._password.value or ""
        if not email or not password:
            self.set_error("Email and password are required.")
            self._shell.refresh_chrome()
            return
        self.set_error("")
        self._shell.run(self._submit_credentials(email, password))

    async def _submit_credentials(self, email: str, password: str) -> None:
        self._submit.disabled = True
        self._shell.refresh_chrome()
        try:
            if self._signing_up:
                name = (self._name.value or "").strip() or email.split("@")[0]
                await self._shell.state.sign_up(email, password, name)
            else:
                await self._shell.state.sign_in(email, password)
        except (AuthError, ApiError) as exc:
            self.set_error(exc.message)
        except Exception as exc:
            self.set_error(f"Could not reach the server: {exc}")
        finally:
            self._submit.disabled = False
            self._password.value = ""
            self._shell.refresh_chrome()

    def set_error(self, message: str) -> None:
        self._error.value = message
        self._error.visible = bool(message)

    def refresh(self) -> None:
        """Re-render against the current palette (theme switch)."""
        self.set_error(self._error.value or "")
