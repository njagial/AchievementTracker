"""Runtime configuration (pydantic-settings).

Every value can be overridden through the environment with the ``TROPHYCASE_`` prefix or
through a ``.env`` file next to the working directory.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings", "reset_settings"]

_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


class Settings(BaseSettings):
    """Application settings."""

    model_config = SettingsConfigDict(
        env_prefix="TROPHYCASE_",
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- core -------------------------------------------------------------------
    env: Literal["development", "test", "production"] = "development"
    debug: bool = False
    secret_key: SecretStr = SecretStr("development-only-secret-key-change-me")
    algorithm: str = "HS256"
    access_token_ttl_seconds: int = Field(default=900, ge=60)
    refresh_token_ttl_seconds: int = Field(default=2_592_000, ge=3600)

    # --- persistence ------------------------------------------------------------
    # 127.0.0.1 rather than "localhost": the latter may resolve to ::1 first, and a black
    # holed IPv6 loopback socket is indistinguishable from a stalled database handshake.
    database_url: str = "postgresql+asyncpg://trophycase:trophycase@127.0.0.1:5432/trophycase"
    database_echo: bool = False

    # --- redis ------------------------------------------------------------------
    redis_url: str = "redis://127.0.0.1:6379/0"
    redis_stream_key: str = "trophycase:events"
    redis_consumer_group: str = "trophycase-evaluators"

    # --- realtime / rate limits -------------------------------------------------
    ws_heartbeat_seconds: int = Field(default=30, ge=5)
    rate_limit_per_minute: int = Field(default=120, ge=1)
    rate_limit_login_per_minute: int = Field(default=10, ge=1)

    # --- TLS --------------------------------------------------------------------
    ca_bundle: str | None = Field(
        default=None,
        description="Path to a PEM CA bundle used for every outbound HTTPS/WSS call.",
    )

    # --- OAuth2 providers -------------------------------------------------------
    oauth_google_client_id: str = ""
    oauth_google_client_secret: SecretStr = SecretStr("")
    oauth_redirect_uri: str = "http://localhost:8000/v1/auth/oauth/google/callback"

    # --- client defaults --------------------------------------------------------
    api_base_url: str = "http://127.0.0.1:8000"
    ws_url: str = "ws://127.0.0.1:8000/v1/ws"
    client_data_dir: str = ""

    # --- validators -------------------------------------------------------------
    @field_validator("ca_bundle", mode="before")
    @classmethod
    def _empty_ca_bundle_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def _check_production_secrets(self) -> Settings:
        if self.env == "production":
            if self.secret_key.get_secret_value() == "development-only-secret-key-change-me":
                raise ValueError("TROPHYCASE_SECRET_KEY must be set in production")
            if self.debug:
                raise ValueError("TROPHYCASE_DEBUG must be false in production")
        return self

    # --- derived ----------------------------------------------------------------
    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def tls_verify(self) -> bool | str:
        """Value for ``httpx``/``websockets`` ``verify=``. Never ``False``."""
        return self.ca_bundle or True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def reset_settings() -> None:
    """Drop the cached settings (tests use this after mutating the environment)."""
    get_settings.cache_clear()
