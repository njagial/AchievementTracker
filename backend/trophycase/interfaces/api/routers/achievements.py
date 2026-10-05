"""Achievement catalogue endpoints: progress views, unlocks and creation."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from trophycase.application.achievement_service import AchievementProgress, AchievementService
from trophycase.domain.entities import UserAchievement
from trophycase.domain.errors import ConflictError, NotFoundError
from trophycase.domain.rules import dump_rule, parse_rule
from trophycase.infrastructure.db.models import utcnow
from trophycase.infrastructure.db.repositories import AchievementRepository
from trophycase.interfaces.api.deps import CurrentUserDep, DbDep
from trophycase.interfaces.api.schemas import (
    AchievementCreateIn,
    AchievementOut,
    CatalogueOut,
)

router = APIRouter(prefix="/v1", tags=["achievements"])


def _to_out(view: AchievementProgress) -> AchievementOut:
    achievement = view.achievement
    return AchievementOut(
        id=achievement.id,
        slug=achievement.slug,
        title=achievement.title,
        description=achievement.description,
        icon=achievement.icon,
        points=achievement.points,
        current=view.current,
        target=view.target,
        progress=view.progress,
        unlocked=view.unlocked,
        unlocked_at=view.unlocked_at,
        rule=achievement.rule.model_dump(mode="json"),
        state={
            "count": view.state.count,
            "value": view.state.value,
            "streak": view.state.streak,
            "last_day": view.state.last_day.isoformat() if view.state.last_day else None,
        },
    )


@router.get("/achievements", response_model=CatalogueOut)
async def catalogue(user: CurrentUserDep, db: DbDep) -> CatalogueOut:
    service = AchievementService(db)
    views = await service.catalogue(user.id)
    return CatalogueOut(
        achievements=[_to_out(view) for view in views],
        unlocked=sum(1 for view in views if view.unlocked),
        score=user.total_score,
        generated_at=utcnow(),
    )


@router.get("/achievements/{achievement_id}", response_model=AchievementOut)
async def achievement_detail(
    achievement_id: UUID, user: CurrentUserDep, db: DbDep
) -> AchievementOut:
    views = await AchievementService(db).catalogue(user.id)
    for view in views:
        if view.achievement.id == achievement_id:
            return _to_out(view)
    raise NotFoundError("achievement not found")


@router.post("/achievements", response_model=AchievementOut, status_code=status.HTTP_201_CREATED)
async def create_achievement(
    body: AchievementCreateIn, user: CurrentUserDep, db: DbDep
) -> AchievementOut:
    """Create an achievement definition.

    The rule is validated by the domain parser before it is stored, so malformed or
    unknown rule kinds can never reach the evaluation engine.
    """
    rule = parse_rule(body.rule)
    repo = AchievementRepository(db)
    existing = await repo.by_slug(body.slug)
    if existing is not None:
        raise ConflictError(f"achievement slug {body.slug!r} already exists")
    created = await repo.add(
        slug=body.slug,
        title=body.title,
        description=body.description,
        icon=body.icon,
        points=body.points,
        rule_spec=dump_rule(rule),
    )
    views = await AchievementService(db).catalogue(user.id)
    for view in views:
        if view.achievement.id == created.id:
            return _to_out(view)
    raise NotFoundError("achievement was created but could not be read back")


@router.get("/me/unlocks", response_model=list[AchievementOut])
async def recent_unlocks(user: CurrentUserDep, db: DbDep) -> list[AchievementOut]:
    unlocks: list[UserAchievement] = await AchievementService(db).unlocks(user.id)
    views = await AchievementService(db).catalogue(user.id)
    by_id = {view.achievement.id: view for view in views}
    ordered: list[AchievementOut] = []
    for unlock in unlocks:
        view = by_id.get(unlock.achievement_id)
        if view is not None:
            ordered.append(_to_out(view))
    return ordered
