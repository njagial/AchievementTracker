"""In-process registry of open WebSockets.

The registry only holds connections owned by *this* process; cross-instance fan-out
happens through Redis pub/sub (``EventBus.subscribe_user``), so the API tier stays
horizontally scalable.
"""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from fastapi import WebSocket

__all__ = ["ConnectionManager", "connections"]


class ConnectionManager:
    def __init__(self) -> None:
        self._by_user: dict[UUID, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def connect(self, user_id: UUID, websocket: WebSocket) -> int:
        async with self._lock:
            sockets = self._by_user.setdefault(user_id, set())
            sockets.add(websocket)
            return len(sockets)

    async def disconnect(self, user_id: UUID, websocket: WebSocket) -> None:
        async with self._lock:
            sockets = self._by_user.get(user_id)
            if sockets is None:
                return
            sockets.discard(websocket)
            if not sockets:
                self._by_user.pop(user_id, None)

    async def send(self, user_id: UUID, message: dict[str, Any]) -> int:
        """Push a message to every connected device of ``user_id``.

        Returns the number of devices reached; dead sockets are pruned silently so one
        broken tab cannot break notifications for the rest.
        """
        import json

        payload = json.dumps(message, separators=(",", ":"))
        async with self._lock:
            sockets = list(self._by_user.get(user_id, ()))
        delivered = 0
        for socket in sockets:
            try:
                await socket.send_text(payload)
                delivered += 1
            except Exception:  # pragma: no cover - socket already closed
                await self.disconnect(user_id, socket)
        return delivered

    def count_for(self, user_id: UUID) -> int:
        return len(self._by_user.get(user_id, ()))

    @property
    def total(self) -> int:
        return sum(len(sockets) for sockets in self._by_user.values())


connections = ConnectionManager()
