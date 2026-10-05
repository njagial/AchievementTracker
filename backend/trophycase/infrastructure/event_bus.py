"""Redis connection management and the pub/sub + stream event bus."""

from __future__ import annotations

import json
from typing import Any, cast
from uuid import UUID

from redis.asyncio import Redis
from redis.asyncio.client import PubSub
from redis.exceptions import ResponseError

from trophycase.config import get_settings

__all__ = ["EventBus", "close_redis", "get_redis", "set_redis"]

_redis: Redis | None = None


def get_redis() -> Redis:
    """Return the process-wide async Redis client (created lazily)."""
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


def set_redis(client: Redis | None) -> None:
    """Inject a client (tests pass ``fakeredis.aioredis.FakeRedis``)."""
    global _redis
    _redis = client


async def close_redis() -> None:
    """Close the shared client during shutdown."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None


class EventBus:
    """Thin facade over Redis pub/sub (realtime fan-out) and streams (work queue)."""

    def __init__(self, redis: Redis | None = None) -> None:
        self._redis = redis if redis is not None else get_redis()
        settings = get_settings()
        self._stream = settings.redis_stream_key
        self._group = settings.redis_consumer_group

    @staticmethod
    def user_channel(user_id: UUID) -> str:
        return f"user:{user_id}"

    async def publish_user(self, user_id: UUID, message: dict[str, Any]) -> int:
        """Publish one JSON message to every device of ``user_id``."""
        channel = self.user_channel(user_id)
        return int(await self._redis.publish(channel, json.dumps(message, separators=(",", ":"))))

    async def subscribe_user(self, user_id: UUID) -> PubSub:
        """Subscribe the caller's WebSocket to a user's fan-out channel."""
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self.user_channel(user_id))
        return pubsub

    async def push_event(self, *, event_id: UUID, user_id: UUID, event_type: str) -> str:
        """Append an event to the evaluation stream (at-least-once delivery)."""
        return str(
            await self._redis.xadd(
                self._stream,
                {
                    "event_id": str(event_id),
                    "user_id": str(user_id),
                    "event_type": event_type,
                },
                maxlen=100_000,
                approximate=True,
            )
        )

    async def ensure_consumer_group(self) -> None:
        """Create the consumer group if it does not exist yet (idempotent)."""
        try:
            await self._redis.xgroup_create(self._stream, self._group, id="0", mkstream=True)
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def read_events(
        self, consumer: str, *, count: int = 32, block_ms: int = 2000
    ) -> list[tuple[str, dict[str, str]]]:
        """Read pending events for ``consumer`` from its group."""
        raw = cast(
            Any,
            await self._redis.xreadgroup(
                groupname=self._group,
                consumername=consumer,
                streams={self._stream: ">"},
                count=count,
                block=block_ms,
            ),
        )
        entries: list[tuple[str, dict[str, str]]] = []
        for stream_entry in raw or []:
            _stream_name, messages = stream_entry
            for message_id, fields in messages or []:
                entries.append((str(message_id), dict(fields)))
        return entries

    async def ack(self, message_id: str) -> int:
        """Acknowledge a processed stream message."""
        return int(await self._redis.xack(self._stream, self._group, message_id))

    async def claim_stale(
        self, consumer: str, *, min_idle_ms: int = 60_000, count: int = 16
    ) -> list[tuple[str, dict[str, str]]]:
        """Reclaim messages another consumer left un-acknowledged (crash recovery)."""
        try:
            _next_start, messages, _deleted = await self._redis.xautoclaim(
                name=self._stream,
                groupname=self._group,
                consumername=consumer,
                min_idle_time=min_idle_ms,
                start_id="0-0",
                count=count,
            )
        except ResponseError:
            return []
        return [(message_id, dict(fields)) for message_id, fields in messages or []]
