"""Typed HTTP client for the TrophyCase API (registration, sync, reads)."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import uuid4

import httpx

from trophycase_client.config import ClientConfig

__all__ = ["ApiClient", "ApiError", "AuthError", "TokenStore"]


class TokenStore(Protocol):
    """Persistence for tokens and the signed-in user (implemented by ``LocalStore``)."""

    def get_meta(self, key: str, default: str | None = None) -> str | None: ...

    def set_meta(self, key: str, value: str) -> None: ...


class ApiError(Exception):
    """A non-2xx response the caller can surface to the user."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class AuthError(ApiError):
    """Credentials are missing, expired or rejected."""


@dataclass(frozen=True, slots=True)
class AuthSession:
    access_token: str
    refresh_token: str
    expires_in: int


class ApiClient:
    """Thin, retry-aware wrapper around ``httpx.AsyncClient``.

    Access tokens are read from / written to the :class:`TokenStore` so a restart keeps
    the session, and a 401 transparently triggers one refresh-and-retry cycle.
    """

    def __init__(self, config: ClientConfig, tokens: TokenStore) -> None:
        self._config = config
        self._tokens = tokens
        self._client = httpx.AsyncClient(
            base_url=config.api_base_url,
            timeout=config.request_timeout_seconds,
            verify=config.verify,  # never False: certificate verification stays on
            headers={"User-Agent": "TrophyCase-Client/1.0"},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- token plumbing -----------------------------------------------------------
    @property
    def access_token(self) -> str | None:
        return self._tokens.get_meta("access_token")

    @property
    def refresh_token_value(self) -> str | None:
        return self._tokens.get_meta("refresh_token")

    @property
    def is_authenticated(self) -> bool:
        return bool(self.access_token)

    def store_session(self, payload: Mapping[str, Any]) -> None:
        self._tokens.set_meta("access_token", str(payload["access_token"]))
        self._tokens.set_meta("refresh_token", str(payload["refresh_token"]))
        self._tokens.set_meta("session_id", str(payload.get("session_id", "")))
        self._tokens.set_meta("user", _dumps(payload.get("user", {})))

    def clear_session(self) -> None:
        for key in ("access_token", "refresh_token", "session_id", "user"):
            self._tokens.set_meta(key, "")

    # --- request helpers ----------------------------------------------------------
    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: Mapping[str, Any] | None = None,
        params: Mapping[str, Any] | None = None,
        auth: bool = True,
        retry_on_401: bool = True,
    ) -> Any:
        headers: dict[str, str] = {}
        if auth:
            token = self.access_token
            if not token:
                raise AuthError(401, "not_authenticated", "sign in to continue")
            headers["Authorization"] = f"Bearer {token}"
        response = await self._client.request(
            method, path, json=json, params=params, headers=headers
        )
        if response.status_code == 401 and auth and retry_on_401:
            if await self._refresh_tokens():
                return await self._request(
                    method, path, json=json, params=params, auth=auth, retry_on_401=False
                )
            self.clear_session()
            raise AuthError(401, "session_expired", "your session expired, sign in again")
        return self._raise_or_json(response)

    @staticmethod
    def _raise_or_json(response: httpx.Response) -> Any:
        if response.is_success:
            if response.status_code == 204 or not response.content:
                return {}
            return response.json()
        code, message = "error", response.text[:300]
        try:
            body = response.json()
        except ValueError:
            body = {}
        if isinstance(body, dict):
            code = str(body.get("code", code))
            message = str(body.get("message", message))
        if response.status_code in {401, 403}:
            raise AuthError(response.status_code, code, message)
        raise ApiError(response.status_code, code, message)

    async def _refresh_tokens(self) -> bool:
        refresh = self.refresh_token_value
        if not refresh:
            return False
        try:
            payload = await self._request(
                "POST",
                "/v1/auth/refresh",
                json={
                    "refresh_token": refresh,
                    "device_id": self._device_id(),
                    "device_name": self._config.device_name,
                },
                auth=False,
                retry_on_401=False,
            )
        except ApiError:
            return False
        self.store_session(payload)
        return True

    def _device_id(self) -> str:
        device = self._tokens.get_meta("device_id")
        return device or uuid4().hex[:32]

    async def _dict(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """``_request`` narrowed to a JSON object (non-objects become ``{}``)."""
        payload = await self._request(method, path, **kwargs)
        return payload if isinstance(payload, dict) else {}

    async def _rows(self, method: str, path: str, **kwargs: Any) -> list[dict[str, Any]]:
        payload = await self._request(method, path, **kwargs)
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    # --- auth ----------------------------------------------------------------------
    async def _auth(self, path: str, body: Mapping[str, Any]) -> dict[str, Any]:
        payload = await self._dict("POST", path, json=body, auth=False, retry_on_401=False)
        self.store_session(payload)
        return payload

    async def register(self, *, email: str, password: str, display_name: str) -> dict[str, Any]:
        return await self._auth(
            "/v1/auth/register",
            {
                "email": email,
                "password": password,
                "display_name": display_name,
                "device_id": self._device_id(),
                "device_name": self._config.device_name,
            },
        )

    async def login(self, *, email: str, password: str) -> dict[str, Any]:
        return await self._auth(
            "/v1/auth/login",
            {
                "email": email,
                "password": password,
                "device_id": self._device_id(),
                "device_name": self._config.device_name,
            },
        )

    async def logout(self) -> None:
        with contextlib.suppress(ApiError):
            # Best effort: the local session is dropped either way.
            await self._request("POST", "/v1/auth/logout")
        self.clear_session()

    async def me(self) -> dict[str, Any]:
        return await self._dict("GET", "/v1/auth/me")

    # --- data ----------------------------------------------------------------------
    async def catalogue(self) -> dict[str, Any]:
        return await self._dict("GET", "/v1/achievements")

    async def push_ops(self, ops: list[dict[str, Any]]) -> dict[str, Any]:
        return await self._dict(
            "POST",
            "/v1/sync/batch",
            json={"client_id": self._device_id(), "ops": ops},
        )

    async def pull_changes(self, since: str | None, limit: int = 200) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if since:
            params["since"] = since
        return await self._dict("GET", "/v1/sync/changes", params=params)

    async def leaderboard(self, scope: str = "global") -> dict[str, Any]:
        return await self._dict("GET", f"/v1/leaderboards/{scope}")

    async def friends(self) -> list[dict[str, Any]]:
        return await self._rows("GET", "/v1/friends")

    async def create_achievement(
        self,
        *,
        slug: str,
        title: str,
        description: str,
        icon: str,
        points: int,
        rule: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._dict(
            "POST",
            "/v1/achievements",
            json={
                "slug": slug,
                "title": title,
                "description": description,
                "icon": icon,
                "points": points,
                "rule": rule,
            },
        )

    async def health(self) -> bool:
        try:
            response = await self._client.get("/healthz")
        except httpx.HTTPError:
            return False
        return response.is_success


def _dumps(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"))
