"""Authentication API: registration, login, refresh rotation and session revocation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

RegisterFn = Callable[..., Awaitable[dict[str, Any]]]
HeadersFn = Callable[[dict[str, Any]], dict[str, str]]


async def test_register_returns_tokens_and_a_profile(
    client: httpx.AsyncClient, register: RegisterFn
) -> None:
    payload = await register(display_name="Ada")

    assert payload["token_type"] == "bearer"
    assert payload["expires_in"] > 0
    assert payload["user"]["display_name"] == "Ada"
    assert payload["user"]["total_score"] == 0
    assert "password" not in payload["user"]
    assert "password_hash" not in payload["user"]


async def test_password_hash_is_stored_but_never_sent(
    client: httpx.AsyncClient, register: RegisterFn, headers: HeadersFn
) -> None:
    payload = await register(password="correct horse battery")

    response = await client.get("/v1/auth/me", headers=headers(payload))

    assert response.status_code == 200
    body = response.json()
    assert body["email"] == payload["user"]["email"]
    assert "password" not in body
    assert "password_hash" not in body


async def test_duplicate_email_conflicts(client: httpx.AsyncClient, register: RegisterFn) -> None:
    payload = await register()

    again = await client.post(
        "/v1/auth/register",
        json={
            "email": payload["user"]["email"],
            "password": "correct horse battery",
            "display_name": "Impostor",
        },
    )

    assert again.status_code == 409
    assert again.json()["code"] == "conflict"


async def test_password_too_short_is_rejected(client: httpx.AsyncClient) -> None:
    response = await client.post(
        "/v1/auth/register",
        json={"email": "short@example.com", "password": "short", "display_name": "S"},
    )
    assert response.status_code == 422


async def test_login_rejects_a_wrong_password(
    client: httpx.AsyncClient, register: RegisterFn
) -> None:
    payload = await register(password="correct horse battery")

    response = await client.post(
        "/v1/auth/login",
        json={"email": payload["user"]["email"], "password": "wrong password entirely"},
    )

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_login_creates_a_second_device_session(
    client: httpx.AsyncClient, register: RegisterFn, headers: HeadersFn
) -> None:
    payload = await register(device_id="laptop")

    login = await client.post(
        "/v1/auth/login",
        json={
            "email": payload["user"]["email"],
            "password": "correct horse battery",
            "device_id": "phone",
            "device_name": "Pixel",
        },
    )

    assert login.status_code == 200
    sessions = await client.get("/v1/auth/sessions", headers=headers(payload))
    assert sessions.status_code == 200
    assert {item["device_id"] for item in sessions.json()} == {"laptop", "phone"}


async def test_me_requires_a_bearer_token(client: httpx.AsyncClient) -> None:
    assert (await client.get("/v1/auth/me")).status_code == 401
    assert (
        await client.get("/v1/auth/me", headers={"Authorization": "Bearer not-a-token"})
    ).status_code == 401


async def test_refresh_rotates_the_token_and_kills_the_old_one(
    client: httpx.AsyncClient, register: RegisterFn
) -> None:
    payload = await register()

    first = await client.post("/v1/auth/refresh", json={"refresh_token": payload["refresh_token"]})
    assert first.status_code == 200
    assert first.json()["refresh_token"] != payload["refresh_token"]

    replay = await client.post("/v1/auth/refresh", json={"refresh_token": payload["refresh_token"]})
    assert replay.status_code == 401, "a rotated refresh token must be single-use"


async def test_logout_revokes_the_current_session(
    client: httpx.AsyncClient, register: RegisterFn, headers: HeadersFn
) -> None:
    payload = await register()

    logout = await client.post("/v1/auth/logout", headers=headers(payload))
    assert logout.status_code == 200

    me = await client.get("/v1/auth/me", headers=headers(payload))
    assert me.status_code == 401


async def test_deleting_another_session_is_a_404(
    client: httpx.AsyncClient, register: RegisterFn, headers: HeadersFn
) -> None:
    payload = await register()

    response = await client.delete(
        "/v1/auth/sessions/01900000-0000-7000-8000-000000000000", headers=headers(payload)
    )

    assert response.status_code == 404
