"""Achievement evaluation use cases (the dynamic rule engine's application facade)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from trophycase.domain.entities import Achievement, UserAchievement, UserEvent
from trophycase.domain.rules import ProgressState, apply_event, evaluate
from trophycase.infrastructure.db.mappers import progress_to_state
from trophycase.infrastructure.db.models import utcnow
from trophycase.infrastructure.db.repositories import (
    AchievementRepository,
    ProgressRepository,
    UserAchievementRepository,
    UserRepository,
)

__all__ = [
    "AchievementProgress",
    "AchievementService",
    "EvaluationReport",
]


@dataclass(frozen=True, slots=True)
class AchievementProgress:
    """Progress view-model for one achievement."""

    achievement: Achievement
    current: float
    target: float
    progress: float
    unlocked: bool
    unlocked_at: datetime | None
    state: ProgressState


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Everything the worker needs after folding one event into the database."""

    event_id: UUID
    user_id: UUID
    messages: list[dict[str, Any]]
    unlocked: list[Achievement]
    score: int | None


class AchievementService:
    def __init__(self, session: AsyncSession) -> None:
        self._achievements = AchievementRepository(session)
        self._progress = ProgressRepository(session)
        self._unlocks = UserAchievementRepository(session)
        self._users = UserRepository(session)

    async def _recompute_score(self, user_id: UUID) -> int:
        unlocks = await self._unlocks.list_for_user(user_id)
        rows = await self._achievements.list_by_ids([u.achievement_id for u in unlocks])
        score = sum(row.points for row in rows)
        await self._users.set_score(user_id, score)
        return score

    async def evaluate_event(self, event: UserEvent) -> EvaluationReport:
        """Fold ``event`` into every active achievement and report resulting messages.

        Pure domain logic lives in :mod:`trophycase.domain.rules`; this method is only
        orchestration: read state -> evaluate -> persist -> describe what to publish.
        """
        achievements = await self._achievements.list_active()
        messages: list[dict[str, Any]] = []
        unlocked_now: list[Achievement] = []
        score: int | None = None

        for achievement in achievements:
            log = await self._progress.get(event.user_id, achievement.id)
            state = progress_to_state(log)
            result = apply_event(
                achievement.rule,
                state,
                event_type=event.event_type,
                payload=event.payload,
                occurred_at=event.occurred_at,
            )

            if result.state != state:
                await self._progress.upsert(
                    user_id=event.user_id,
                    achievement_id=achievement.id,
                    count=result.state.count,
                    value=result.state.value,
                    streak=result.state.streak,
                    last_day=result.state.last_day,
                    updated_at=utcnow(),
                )
                messages.append(
                    {
                        "type": "progress.updated",
                        "achievement_id": str(achievement.id),
                        "slug": achievement.slug,
                        "title": achievement.title,
                        "icon": achievement.icon,
                        "current": result.current,
                        "target": result.target,
                        "progress": result.progress,
                        "completed": result.completed,
                    }
                )

            if result.completed and await self._unlocks.get(event.user_id, achievement.id) is None:
                inserted = await self._unlocks.add(
                    user_id=event.user_id,
                    achievement_id=achievement.id,
                    current=result.current,
                    target=result.target,
                )
                if inserted is None:
                    continue
                unlocked_now.append(achievement)
                score = await self._recompute_score(event.user_id)
                messages.append(
                    {
                        "type": "achievement.unlocked",
                        "achievement_id": str(achievement.id),
                        "slug": achievement.slug,
                        "title": achievement.title,
                        "description": achievement.description,
                        "icon": achievement.icon,
                        "points": achievement.points,
                        "unlocked_at": inserted.unlocked_at.isoformat(),
                    }
                )

        if score is not None:
            messages.append(
                {
                    "type": "leaderboard.changed",
                    "user_id": str(event.user_id),
                    "score": score,
                }
            )

        return EvaluationReport(
            event_id=event.id,
            user_id=event.user_id,
            messages=messages,
            unlocked=unlocked_now,
            score=score,
        )

    async def catalogue(self, user_id: UUID) -> list[AchievementProgress]:
        """All active achievements with the caller's progress and unlock state."""
        achievements = await self._achievements.list_active()
        logs = {row.achievement_id: row for row in await self._progress.list_for_user(user_id)}
        unlocks = {row.achievement_id: row for row in await self._unlocks.list_for_user(user_id)}
        views: list[AchievementProgress] = []
        for achievement in achievements:
            state = progress_to_state(logs.get(achievement.id))
            evaluated = evaluate(achievement.rule, state)
            unlock = unlocks.get(achievement.id)
            views.append(
                AchievementProgress(
                    achievement=achievement,
                    current=evaluated.current,
                    target=evaluated.target,
                    progress=evaluated.progress,
                    unlocked=unlock is not None or evaluated.completed,
                    unlocked_at=unlock.unlocked_at if unlock is not None else None,
                    state=evaluated.state,
                )
            )
        return views

    async def unlocks(self, user_id: UUID) -> list[UserAchievement]:
        return await self._unlocks.list_for_user(user_id)
