"""End-to-end achievement flow: define a rule, emit events, let the worker unlock it."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import httpx

from trophycase.worker.main import EventWorker

RegisterFn = Callable[..., Awaitable[dict[str, Any]]]
HeadersFn = Callable[[dict[str, Any]], dict[str, str]]


async def _create_achievement(
    client: httpx.AsyncClient, headers: dict[str, str], **overrides: Any
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "slug": overrides.pop("slug", f"first-{uuid4().hex[:8]}"),
        "title": "First Steps",
        "rule": {"kind": "counter", "event_type": "task.completed", "count": 2},
    }
    body.update(overrides)
    response = await client.post("/v1/achievements", headers=headers, json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def test_creating_an_achievement_returns_zero_progress(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    created = await _create_achievement(client, headers(registered_user))

    assert created["current"] == 0
    assert created["target"] == 2
    assert created["progress"] == 0
    assert created["unlocked"] is False


async def test_duplicate_slugs_conflict(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    h = headers(registered_user)
    await _create_achievement(client, h, slug="daily-deeds")

    response = await client.post(
        "/v1/achievements",
        headers=h,
        json={
            "slug": "daily-deeds",
            "title": "Copycat",
            "rule": {"kind": "boolean", "event_type": "a"},
        },
    )

    assert response.status_code == 409


async def test_malformed_rules_are_rejected_before_storage(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    response = await client.post(
        "/v1/achievements",
        headers=headers(registered_user),
        json={"slug": "broken", "title": "Broken", "rule": {"kind": "telepathy"}},
    )

    assert response.status_code == 422
    assert response.json()["code"] == "invalid_rule"


async def test_catalogue_lists_every_active_achievement(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    h = headers(registered_user)
    created = await _create_achievement(client, h)

    catalogue = await client.get("/v1/achievements", headers=h)

    assert catalogue.status_code == 200
    body = catalogue.json()
    assert [item["slug"] for item in body["achievements"]] == [created["slug"]]
    assert body["unlocked"] == 0
    assert body["score"] == 0


async def test_worker_unlocks_and_scores_an_achievement(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    h = headers(registered_user)
    achievement = await _create_achievement(client, h, points=15)
    for _ in range(2):
        accepted = await client.post(
            "/v1/events",
            headers=h,
            json={"event_type": "task.completed", "payload": {"task_id": "t-1"}},
        )
        assert accepted.status_code == 202

    await EventWorker(consumer="test").drain()

    catalogue = await client.get("/v1/achievements", headers=h)
    detail = next(
        item for item in catalogue.json()["achievements"] if item["id"] == achievement["id"]
    )
    assert detail["current"] == 2
    assert detail["unlocked"] is True
    assert detail["progress"] == 1
    assert catalogue.json()["unlocked"] == 1
    assert catalogue.json()["score"] == 15

    unlocks = await client.get("/v1/me/unlocks", headers=h)
    assert [item["id"] for item in unlocks.json()] == [achievement["id"]]


async def test_replaying_an_event_never_double_counts(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    h = headers(registered_user)
    achievement = await _create_achievement(client, h)
    event_id = str(uuid4())
    body = {"event_type": "task.completed", "event_id": event_id}

    await client.post("/v1/events", headers=h, json={**body, "seq": 1})
    await client.post("/v1/events", headers=h, json={**body, "seq": 1})
    await EventWorker(consumer="test").drain()

    catalogue = await client.get("/v1/achievements", headers=h)
    detail = next(
        item for item in catalogue.json()["achievements"] if item["id"] == achievement["id"]
    )
    assert detail["current"] == 1, "a replayed event must not advance progress twice"


async def test_event_of_another_type_does_not_count(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    h = headers(registered_user)
    achievement = await _create_achievement(client, h)

    await client.post("/v1/events", headers=h, json={"event_type": "session.opened"})
    await EventWorker(consumer="test").drain()

    catalogue = await client.get("/v1/achievements", headers=h)
    detail = next(
        item for item in catalogue.json()["achievements"] if item["id"] == achievement["id"]
    )
    assert detail["current"] == 0
