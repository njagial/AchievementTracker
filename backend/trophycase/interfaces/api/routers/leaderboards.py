"""Leaderboard endpoints (global and friends-only)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from trophycase.application.leaderboard_service import LeaderboardService
from trophycase.infrastructure.db.models import utcnow
from trophycase.interfaces.api.deps import CurrentUserDep, DbDep
from trophycase.interfaces.api.schemas import LeaderboardEntryOut, LeaderboardOut

router = APIRouter(prefix="/v1/leaderboards", tags=["leaderboards"])


@router.get("/{scope}", response_model=LeaderboardOut)
async def leaderboard(
    scope: str, user: CurrentUserDep, db: DbDep, limit: int = Query(default=50, ge=1, le=200)
) -> LeaderboardOut:
    if scope not in {"global", "friends"}:
        raise HTTPException(status_code=404, detail="unknown leaderboard scope")
    service = LeaderboardService(db)
    entries = (
        await service.top(limit=limit)
        if scope == "global"
        else await service.top_friends(user.id, limit=limit)
    )
    return LeaderboardOut(
        scope=scope,  # type: ignore[arg-type]
        entries=[
            LeaderboardEntryOut(
                rank=entry.rank,
                user_id=entry.user_id,
                display_name=entry.display_name,
                score=entry.score,
                avatar_url=entry.avatar_url,
                you=entry.user_id == user.id,
            )
            for entry in entries
        ],
        generated_at=utcnow(),
    )
