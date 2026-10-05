"""Event ingestion: idempotency, batching and authorisation."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import uuid4

import httpx

RegisterFn = Callable[..., Awaitable[dict[str, Any]]]
HeadersFn = Callable[[dict[str, Any]], dict[str, str]]


async def test_ingesting_an_event_returns_202(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    event_id = str(uuid4())
    response = await client.post(
        "/v1/events",
        headers=headers(registered_user),
        json={
            "event_type": "task.completed",
            "payload": {"task_id": "t-1"},
            "event_id": event_id,
            "client_id": "dev-1",
            "seq": 1,
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["event_id"] == event_id
    assert body["duplicate"] is False


async def test_replaying_the_same_event_id_is_idempotent(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    event_id = str(uuid4())
    body = {"event_type": "task.completed", "payload": {}, "event_id": event_id, "seq": 1}

    first = await client.post("/v1/events", headers=headers(registered_user), json=body)
    second = await client.post("/v1/events", headers=headers(registered_user), json=body)

    assert first.json()["duplicate"] is False
    assert second.json()["duplicate"] is True
    assert second.json()["event_id"] == event_id


async def test_event_ids_are_generated_when_the_client_has_none(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    response = await client.post(
        "/v1/events", headers=headers(registered_user), json={"event_type": "session.opened"}
    )

    assert response.status_code == 202
    assert response.json()["event_id"]


async def test_batch_ingestion_counts_only_new_events(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    payload = {
        "events": [
            {"event_type": "task.completed", "event_id": str(uuid4()), "seq": 1},
            {"event_type": "task.completed", "event_id": str(uuid4()), "seq": 2},
        ]
    }

    first = await client.post("/v1/events/batch", headers=headers(registered_user), json=payload)
    replay = await client.post("/v1/events/batch", headers=headers(registered_user), json=payload)

    assert first.status_code == 202
    assert first.json()["accepted"] == 2
    assert replay.json()["accepted"] == 0
    assert [item["duplicate"] for item in replay.json()["results"]] == [True, True]


async def test_events_require_authentication(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/events", json={"event_type": "session.opened"})
    assert response.status_code == 401


async def test_a_recorded_event_id_is_always_a_no_op(
    client: httpx.AsyncClient,
    register: RegisterFn,
    headers: HeadersFn,
) -> None:
    """Event ids are the idempotency key, and they are unique across the whole table."""
    alice = await register()
    mallory = await register()
    event_id = str(uuid4())

    await client.post(
        "/v1/events",
        headers=headers(alice),
        json={"event_type": "task.completed", "event_id": event_id, "seq": 1},
    )
    replay = await client.post(
        "/v1/events",
        headers=headers(mallory),
        json={"event_type": "task.completed", "event_id": event_id, "seq": 1},
    )

    assert replay.json()["duplicate"] is True


async def test_the_worker_is_summoned_only_after_the_row_is_committed(
    client: httpx.AsyncClient,
    registered_user: dict[str, Any],
    headers: HeadersFn,
    published_visibility: list[bool],
) -> None:
    """A stream message that races its own transaction loses the event forever."""
    response = await client.post(
        "/v1/events", headers=headers(registered_user), json={"event_type": "task.completed"}
    )

    assert response.status_code == 202
    assert published_visibility == [True]
