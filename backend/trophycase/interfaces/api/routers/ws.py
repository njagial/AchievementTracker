"""``/v1/ws`` - the realtime channel every signed-in device keeps open."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from trophycase.config import get_settings
from trophycase.domain.errors import UnauthorizedError
from trophycase.infrastructure.db.repositories import DeviceSessionRepository
from trophycase.infrastructure.db.session import get_session_factory
from trophycase.infrastructure.event_bus import EventBus
from trophycase.infrastructure.security import decode_token
from trophycase.interfaces.ws.gateway import connections

router = APIRouter(tags=["realtime"])

_PING = "ping"


async def _send(socket: WebSocket, payload: dict[str, Any]) -> None:
    await socket.send_text(json.dumps(payload, separators=(",", ":")))


async def _pump_pubsub(socket: WebSocket, user_id: UUID) -> None:
    bus = EventBus()
    pubsub = await bus.subscribe_user(user_id)
    try:
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            data = message.get("data")
            if isinstance(data, str):
                await socket.send_text(data)
    finally:
        # redis-py ships no annotations for this coroutine yet.
        await pubsub.aclose()  # type: ignore[no-untyped-call]


async def _pump_client(socket: WebSocket) -> None:
    while True:
        text = await socket.receive_text()
        if text == _PING:
            await _send(socket, {"type": "pong"})
        else:  # ignore unknown client frames but keep the socket alive
            await _send(socket, {"type": "ack", "frame": text[:64]})


async def _heartbeat(socket: WebSocket) -> None:
    settings = get_settings()
    while True:
        await asyncio.sleep(settings.ws_heartbeat_seconds)
        await _send(socket, {"type": "heartbeat"})


@router.websocket("/v1/ws")
async def websocket_endpoint(
    websocket: WebSocket,
    token: str = Query(..., min_length=10, max_length=4096),
) -> None:
    """Authenticate, then bridge Redis pub/sub to this device."""
    try:
        claims = decode_token(token, expected_type="access")
    except UnauthorizedError:
        await websocket.close(code=1008)
        return

    factory = get_session_factory()
    async with factory() as db:
        session = await DeviceSessionRepository(db).get(claims.session_id)
    if session is None or session.revoked_at is not None or session.user_id != claims.user_id:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    user_id = claims.user_id
    connections_count = await connections.connect(user_id, websocket)

    tasks: list[asyncio.Task[None]] = []
    try:
        await _send(
            websocket,
            {"type": "connected", "user_id": str(user_id), "connections": connections_count},
        )
        tasks = [
            asyncio.create_task(_pump_pubsub(websocket, user_id), name=f"pubsub:{user_id}"),
            asyncio.create_task(_pump_client(websocket), name=f"client:{user_id}"),
            asyncio.create_task(_heartbeat(websocket), name=f"beat:{user_id}"),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        for task in done:
            error = task.exception()
            if error is not None and not isinstance(error, WebSocketDisconnect):
                raise error
    except WebSocketDisconnect:
        pass
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await connections.disconnect(user_id, websocket)
        if websocket.client_state.name == "CONNECTED":  # pragma: no cover - defensive
            await websocket.close(code=1000)
