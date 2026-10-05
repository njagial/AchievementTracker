"""Client-side configuration: where data lives and where the API is."""

from __future__ import annotations

import os
import platform
from dataclasses import dataclass
from pathlib import Path

from trophycase.config import get_settings

__all__ = ["ClientConfig", "default_data_dir"]


def default_data_dir() -> Path:
    """Per-user, per-OS directory for the offline database."""
    system = platform.system()
    if system == "Windows":
        root = os.getenv("LOCALAPPDATA")
        base = Path(root) if root else Path.home() / "AppData" / "Local"
        return base / "TrophyCase"
    if system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "TrophyCase"
    xdg = os.getenv("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "trophycase"


@dataclass(frozen=True, slots=True)
class ClientConfig:
    """Everything the client needs to talk to the API and store data offline."""

    api_base_url: str
    ws_url: str
    device_name: str
    data_dir: Path
    #: ``True`` (system CAs) or a path to a PEM bundle. Never ``False``.
    verify: bool | str
    sync_interval_seconds: float = 5.0
    request_timeout_seconds: float = 15.0

    @property
    def database_path(self) -> Path:
        return self.data_dir / "trophycase.db"

    @classmethod
    def load(cls, data_dir: Path | None = None) -> ClientConfig:
        """Build config from :class:`Settings` plus optional overrides."""
        settings = get_settings()
        directory = data_dir or (
            Path(settings.client_data_dir) if settings.client_data_dir else default_data_dir()
        )
        directory.mkdir(parents=True, exist_ok=True)
        return cls(
            api_base_url=settings.api_base_url.rstrip("/"),
            ws_url=settings.ws_url,
            device_name=platform.node() or "TrophyCase client",
            data_dir=directory,
            verify=settings.tls_verify,
        )
