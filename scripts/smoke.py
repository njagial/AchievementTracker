"""End-to-end smoke test against a running TrophyCase stack.

Verifies: register -> create achievement -> realtime push -> rule evaluation ->
idempotent sync replay -> cursor pull -> leaderboard.

Usage: python smoke.py <base-url>
"""

from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import UTC, datetime

import httpx
import websockets

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:18000"
WS_BASE = BASE.replace("http://", "ws://").replace("https://", "wss://") + "/v1/ws"

FAILURES: list[str] = []


def check(label: str, ok: bool, detail: object = "") -> None:
    print(f"{'PASS' if ok else 'FAIL'}  {label}  {detail if not ok else ''}")
    if not ok:
        FAILURES.append(label)


async def main() -> None:
    email = f"smoke-{uuid.uuid4().hex[:8]}@example.com"
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as api:
        r = await api.post(
            "/v1/auth/register",
            json={
                "email": email,
                "password": "smoke-test-password",
                "display_name": "Smoke",
                "device_id": "smoke-device",
                "device_name": "smoke",
            },
        )
        check("register", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        auth = r.json()
        headers = {"Authorization": f"Bearer {auth['access_token']}"}
        user = auth["user"]
        check("user.updated_at present for LWW", "updated_at" in user, user)

        r = await api.get("/healthz")
        check("healthz", r.status_code == 200 and r.json().get("database") == "ok", r.text)

        # A unique event type per run so only this run's achievement can match.
        event_type = f"smoke.completed.{uuid.uuid4().hex[:6]}"

        r = await api.post(
            "/v1/achievements",
            headers=headers,
            json={
                "slug": f"smoke-{uuid.uuid4().hex[:6]}",
                "title": "Smoke steps",
                "description": "Two tasks",
                "icon": "trophy",
                "points": 7,
                "rule": {"kind": "counter", "event_type": event_type, "count": 2},
            },
        )
        check("create achievement", r.status_code == 201, f"{r.status_code} {r.text[:200]}")
        achievement = r.json()
        check("rule echoed for offline clients", achievement.get("rule", {}).get("count") == 2)
        check("state echoed for offline clients", "count" in achievement.get("state", {}))
        achievement_id = achievement["id"]

        op_id = str(uuid.uuid4())
        ws_messages: list[dict] = []
        async with websockets.connect(f"{WS_BASE}?token={auth['access_token']}") as ws:
            connected = json.loads(await ws.recv())
            check("ws connected", connected.get("type") == "connected", connected)

            for _ in range(2):
                r = await api.post(
                    "/v1/events",
                    headers=headers,
                    json={"event_type": event_type, "payload": {"n": 1}},
                )
                check("ingest event", r.status_code == 202, f"{r.status_code} {r.text[:200]}")

            deadline = 15.0
            try:
                while {"progress.updated", "achievement.unlocked"} - {
                    m.get("type") for m in ws_messages
                }:
                    raw = await asyncio.wait_for(ws.recv(), timeout=deadline)
                    deadline -= 1.0
                    message = json.loads(raw)
                    if message.get("type") in {"heartbeat", "pong", "ack"}:
                        continue
                    ws_messages.append(message)
                    if deadline <= 0:
                        break
            except TimeoutError:
                pass

        kinds = {m.get("type") for m in ws_messages}
        check("realtime progress.updated", "progress.updated" in kinds, kinds)
        check("realtime achievement.unlocked", "achievement.unlocked" in kinds, kinds)

        # Give the worker a moment, then read the authoritative catalogue.
        for _ in range(20):
            r = await api.get("/v1/achievements", headers=headers)
            catalogue = r.json()
            row = next(a for a in catalogue["achievements"] if a["id"] == achievement_id)
            if row["unlocked"]:
                break
            await asyncio.sleep(0.25)
        check("rule evaluated by worker", row["unlocked"], row)
        check("progress reaches target", row["current"] == 2.0, row)
        check("score updated", catalogue["score"] == 7, catalogue["score"])

        # Offline replay: the same op_id twice must never double count.
        ops = [
            {
                "op_id": op_id,
                "seq": 1,
                "kind": "event",
                "event_type": event_type,
                "payload": {},
                "occurred_at": datetime.now(UTC).isoformat(),
                "fields": {},
                "base_updated_at": None,
            }
        ]
        for attempt in (1, 2):
            r = await api.post(
                "/v1/sync/batch",
                headers=headers,
                json={"client_id": "smoke-device", "ops": ops},
            )
            check(f"sync push #{attempt}", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
            body = r.json()
            if attempt == 1:
                check("first push applied", body["applied"] == 1, body)
            else:
                check("replay is a duplicate", body["duplicates"] == 1, body)

        r = await api.get("/v1/sync/changes", headers=headers)
        check("pull changes", r.status_code == 200 and "cursor" in r.json(), r.text[:200])

        r = await api.get("/v1/leaderboards/global", headers=headers)
        check("leaderboard", r.status_code == 200 and r.json()["entries"], r.text[:200])

        r = await api.get("/v1/me/unlocks", headers=headers)
        check("recent unlocks", r.status_code == 200 and len(r.json()) == 1, r.text[:200])

        r = await api.post("/v1/auth/logout", headers=headers)
        check("logout", r.status_code == 200, f"{r.status_code} {r.text[:200]}")
        r = await api.get("/v1/auth/me", headers=headers)
        check("token revoked after logout", r.status_code == 401, r.status_code)


try:
    asyncio.run(main())
except Exception as exc:
    check("unexpected error", False, repr(exc))

print()
if FAILURES:
    print(f"{len(FAILURES)} check(s) failed: {FAILURES}")
    sys.exit(1)
print("all checks passed")
