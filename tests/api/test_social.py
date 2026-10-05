"""Friends and leaderboards: requests, acceptance and the friends-only ranking."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import httpx

RegisterFn = Callable[..., Awaitable[dict[str, Any]]]
HeadersFn = Callable[[dict[str, Any]], dict[str, str]]


async def test_friend_request_lands_in_the_targets_list(
    client: httpx.AsyncClient,
    register: RegisterFn,
    headers: HeadersFn,
) -> None:
    ada = await register(display_name="Ada")
    grace = await register(email="grace@example.com", display_name="Grace")

    request = await client.post(
        "/v1/friends/request", headers=headers(ada), json={"email": "grace@example.com"}
    )
    inbox = await client.get("/v1/friends", headers=headers(grace))

    assert request.status_code == 201
    assert request.json()["display_name"] == "Grace"
    assert request.json()["status"] == "pending"
    assert [item["display_name"] for item in inbox.json()] == ["Ada"]


async def test_accepting_a_request_creates_the_pair(
    client: httpx.AsyncClient,
    register: RegisterFn,
    headers: HeadersFn,
) -> None:
    ada = await register(display_name="Ada")
    grace = await register(email="grace@example.com", display_name="Grace")
    created = await client.post(
        "/v1/friends/request", headers=headers(ada), json={"email": "grace@example.com"}
    )

    accepted = await client.post(
        f"/v1/friends/{created.json()['id']}/accept", headers=headers(grace)
    )
    mine = await client.get("/v1/friends", headers=headers(ada))
    theirs = await client.get("/v1/friends", headers=headers(grace))

    assert accepted.json()["status"] == "accepted"
    assert [item["status"] for item in mine.json()] == ["accepted"]
    assert [item["status"] for item in theirs.json()] == ["accepted"]


async def test_duplicate_and_self_requests_are_rejected(
    client: httpx.AsyncClient,
    register: RegisterFn,
    headers: HeadersFn,
) -> None:
    ada = await register(display_name="Ada")
    await register(email="grace@example.com")  # the target must exist

    first = await client.post(
        "/v1/friends/request", headers=headers(ada), json={"email": "grace@example.com"}
    )
    again = await client.post(
        "/v1/friends/request", headers=headers(ada), json={"email": "grace@example.com"}
    )
    myself = await client.post(
        "/v1/friends/request",
        headers=headers(ada),
        json={"email": ada["user"]["email"]},
    )
    unknown = await client.post(
        "/v1/friends/request",
        headers=headers(ada),
        json={"email": "nobody@example.com"},
    )

    assert first.status_code == 201
    assert again.status_code == 409
    assert myself.status_code == 422
    assert unknown.status_code == 404


async def test_only_the_addressee_can_accept(
    client: httpx.AsyncClient,
    register: RegisterFn,
    headers: HeadersFn,
) -> None:
    ada = await register(display_name="Ada")
    await register(email="grace@example.com")  # the target must exist
    created = await client.post(
        "/v1/friends/request", headers=headers(ada), json={"email": "grace@example.com"}
    )

    wrong_person = await client.post(
        f"/v1/friends/{created.json()['id']}/accept", headers=headers(ada)
    )
    assert wrong_person.status_code == 404


async def test_global_leaderboard_marks_the_caller(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    response = await client.get("/v1/leaderboards/global", headers=headers(registered_user))

    assert response.status_code == 200
    body = response.json()
    assert body["scope"] == "global"
    assert len(body["entries"]) == 1
    assert body["entries"][0]["you"] is True
    assert body["entries"][0]["rank"] == 1


async def test_friends_leaderboard_never_lists_strangers(
    client: httpx.AsyncClient,
    register: RegisterFn,
    headers: HeadersFn,
) -> None:
    ada = await register(display_name="Ada")
    loner = await register(display_name="Loner")

    # Nobody is friends yet, so each caller only ever sees themself.
    mine = await client.get("/v1/leaderboards/friends", headers=headers(ada))
    theirs = await client.get("/v1/leaderboards/friends", headers=headers(loner))

    assert [entry["display_name"] for entry in mine.json()["entries"]] == ["Ada"]
    assert [entry["display_name"] for entry in theirs.json()["entries"]] == ["Loner"]
    assert mine.json()["entries"][0]["you"] is True


async def test_unknown_leaderboard_scope_is_404(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    response = await client.get("/v1/leaderboards/universe", headers=headers(registered_user))
    assert response.status_code == 404
