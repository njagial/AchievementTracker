"""Offline sync endpoints: batch replay, pull cursors and profile conflicts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx

HeadersFn = Callable[[dict[str, Any]], dict[str, str]]


def _event_op(seq: int) -> dict[str, Any]:
    return {
        "op_id": str(uuid4()),
        "seq": seq,
        "kind": "event",
        "event_type": "task.completed",
        "payload": {"task_id": f"t-{seq}"},
        "occurred_at": datetime.now().isoformat(),
    }


async def test_push_replays_then_deduplicates(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    h = headers(registered_user)
    batch = {"client_id": "dev-1", "ops": [_event_op(1), _event_op(2)]}

    first = await client.post("/v1/sync/batch", headers=h, json=batch)
    second = await client.post("/v1/sync/batch", headers=h, json=batch)

    assert first.status_code == 200
    assert first.json()["applied"] == 2
    assert first.json()["duplicates"] == 0
    assert first.json()["conflicts"] == 0
    assert second.json()["applied"] == 0
    assert second.json()["duplicates"] == 2
    assert [item["status"] for item in second.json()["results"]] == ["duplicate", "duplicate"]


async def test_pushed_events_show_up_in_changes(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    h = headers(registered_user)
    op = _event_op(1)
    await client.post("/v1/sync/batch", headers=h, json={"client_id": "dev-1", "ops": [op]})

    changes = await client.get("/v1/sync/changes", headers=h)

    assert changes.status_code == 200
    body = changes.json()
    assert [item["id"] for item in body["events"]] == [op["op_id"]]
    assert body["has_more"] is False

    since = body["cursor"]
    again = await client.get("/v1/sync/changes", headers=h, params={"since": since})
    assert again.json()["events"] == []


async def test_replayed_events_are_committed_before_the_worker_is_summoned(
    client: httpx.AsyncClient,
    registered_user: dict[str, Any],
    headers: HeadersFn,
    published_visibility: list[bool],
) -> None:
    """Offline replay takes the same path as a live event: row first, stream second."""
    h = headers(registered_user)
    response = await client.post(
        "/v1/sync/batch", headers=h, json={"client_id": "dev-1", "ops": [_event_op(1)]}
    )

    assert response.status_code == 200
    assert response.json()["applied"] == 1
    assert published_visibility == [True]


async def test_changes_never_leak_another_users_events(
    client: httpx.AsyncClient,
    register: Callable[..., Awaitable[dict[str, Any]]],
    headers: HeadersFn,
) -> None:
    mine = await register()
    theirs = await register()
    op = _event_op(1)

    await client.post("/v1/sync/batch", headers=headers(mine), json={"client_id": "a", "ops": [op]})
    changes = await client.get("/v1/sync/changes", headers=headers(theirs))

    assert changes.json()["events"] == []


async def test_profile_update_then_conflicting_stale_write(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    h = headers(registered_user)
    me = await client.get("/v1/auth/me", headers=h)
    base = me.json()["updated_at"]
    fresh: dict[str, Any] = {
        "op_id": str(uuid4()),
        "seq": 1,
        "kind": "profile",
        "fields": {"display_name": "Renamed"},
        "base_updated_at": base,
    }

    applied = await client.post(
        "/v1/sync/batch", headers=h, json={"client_id": "dev-1", "ops": [fresh]}
    )
    stale = await client.post(
        "/v1/sync/batch",
        headers=h,
        json={
            "client_id": "dev-1",
            "ops": [{**fresh, "op_id": str(uuid4()), "fields": {"display_name": "Stale"}}],
        },
    )

    assert applied.json()["results"][0]["status"] == "applied"
    assert stale.json()["results"][0]["status"] == "rejected"
    assert stale.json()["results"][0]["conflict"] is True
    assert stale.json()["results"][0]["detail"]["server"]["display_name"] == "Renamed"

    renamed = await client.get("/v1/auth/me", headers=h)
    assert renamed.json()["display_name"] == "Renamed"


async def test_profile_write_without_a_base_timestamp_always_applies(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    h = headers(registered_user)
    op: dict[str, Any] = {
        "op_id": str(uuid4()),
        "seq": 1,
        "kind": "profile",
        "fields": {"display_name": "Force Write"},
    }

    first = await client.post("/v1/sync/batch", headers=h, json={"client_id": "dev-1", "ops": [op]})
    second = await client.post(
        "/v1/sync/batch", headers=h, json={"client_id": "dev-1", "ops": [op]}
    )

    assert first.json()["results"][0]["status"] == "applied"
    assert second.json()["results"][0]["status"] == "duplicate"


async def test_sync_requires_authentication(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/sync/batch", json={"client_id": "dev", "ops": [_event_op(1)]})
    assert response.status_code == 401


async def test_changes_accepts_an_iso_cursor(
    client: httpx.AsyncClient, registered_user: dict[str, Any], headers: HeadersFn
) -> None:
    cursor = (datetime.now(UTC) - timedelta(days=1)).isoformat()

    response = await client.get(
        "/v1/sync/changes", headers=headers(registered_user), params={"since": cursor}
    )

    assert response.status_code == 200
    assert response.json()["events"] == []
