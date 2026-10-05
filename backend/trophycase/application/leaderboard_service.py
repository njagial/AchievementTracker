"""Leaderboard use cases: Redis ZSET for hot reads, PostgreSQL as the source of truth."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from trophycase.domain.entities import LeaderboardEntry
from trophycase.infrastructure.db.repositories import FriendshipRepository, UserRepository
from trophycase.infrastructure.event_bus import get_redis

__all__ = ["LeaderboardService"]

GLOBAL_KEY = "leaderboard:global"
_MAX_MEMBERS = 1000


class LeaderboardService:
    def __init__(self, session: AsyncSession, redis: Redis | None = None) -> None:
        self._users = UserRepository(session)
        self._friendships = FriendshipRepository(session)
        self._redis: Redis = redis if redis is not None else get_redis()

    async def sync_score(self, user_id: UUID, score: int) -> None:
        """Keep the hot cache in step with a newly computed score."""
        await self._redis.zadd(GLOBAL_KEY, {str(user_id): float(max(0, score))})

    async def _ensure_seeded(self) -> None:
        size = int(await self._redis.zcard(GLOBAL_KEY))
        if size > 0:
            return
        # Empty cache: rebuild from the database (first boot, Redis restart, cache flush).
        entries = await self._load_top_from_db(limit=_MAX_MEMBERS)
        if entries:
            mapping = {str(e.user_id): float(e.score) for e in entries}
            await self._redis.zadd(GLOBAL_KEY, mapping)

    async def _load_top_from_db(self, *, limit: int) -> list[LeaderboardEntry]:
        rows = await self._users.top_scorers(limit=limit)
        return [
            LeaderboardEntry(
                user_id=row.id,
                display_name=row.display_name,
                score=row.total_score,
                rank=index + 1,
                avatar_url=row.avatar_url,
            )
            for index, row in enumerate(rows)
        ]

    async def top(self, *, limit: int = 50) -> list[LeaderboardEntry]:
        """Global top-N by score (redis), falling back to the database."""
        await self._ensure_seeded()
        raw = cast(
            list[tuple[str, float]],
            await self._redis.zrevrange(GLOBAL_KEY, 0, limit - 1, withscores=True),
        )
        if not raw:
            return await self._load_top_from_db(limit=limit)
        user_ids = [UUID(member) for member, _score in raw]
        users = {user.id: user for user in await self._users.list_by_ids(user_ids)}
        entries: list[LeaderboardEntry] = []
        for index, (member, score) in enumerate(raw):
            user = users.get(UUID(member))
            entries.append(
                LeaderboardEntry(
                    user_id=UUID(member),
                    display_name=user.display_name if user else "unknown",
                    score=int(score),
                    rank=index + 1,
                    avatar_url=user.avatar_url if user else None,
                )
            )
        return entries

    async def top_friends(self, user_id: UUID, *, limit: int = 50) -> list[LeaderboardEntry]:
        """Friends-only leaderboard; the caller always appears when they have friends."""
        friends = await self._friendships.friend_ids(user_id)
        friends.add(user_id)
        if not friends:
            return []
        ranked = await self.top(limit=_MAX_MEMBERS)
        filtered = [entry for entry in ranked if entry.user_id in friends][:limit]
        return [
            LeaderboardEntry(
                user_id=entry.user_id,
                display_name=entry.display_name,
                score=entry.score,
                rank=index + 1,
                avatar_url=entry.avatar_url,
            )
            for index, entry in enumerate(filtered)
        ]

    async def rank_of(self, user_id: UUID) -> int | None:
        """1-based global rank for one user."""
        await self._ensure_seeded()
        rank = cast(int | None, await self._redis.zrevrank(GLOBAL_KEY, str(user_id)))
        return int(rank) + 1 if rank is not None else None
