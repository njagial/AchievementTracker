"""Friendship endpoints powering the friends-only leaderboard."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from trophycase.domain.entities import Friendship, FriendshipStatus
from trophycase.domain.errors import ConflictError, NotFoundError, ValidationError
from trophycase.infrastructure.db.repositories import FriendshipRepository, UserRepository
from trophycase.interfaces.api.deps import CurrentUserDep, DbDep
from trophycase.interfaces.api.schemas import FriendOut, FriendRequestIn

router = APIRouter(prefix="/v1/friends", tags=["friends"])


async def _to_out(friendship: Friendship, *, viewer_id: UUID, db: object) -> FriendOut:
    other_id = (
        friendship.addressee_id if friendship.requester_id == viewer_id else friendship.requester_id
    )
    other = await UserRepository(db).get(other_id)  # type: ignore[arg-type]
    return FriendOut(
        id=friendship.id,
        user_id=other_id,
        display_name=other.display_name if other else "unknown",
        status=friendship.status.value,
    )


@router.get("", response_model=list[FriendOut])
async def list_friends(user: CurrentUserDep, db: DbDep) -> list[FriendOut]:
    friendships = await FriendshipRepository(db).list_for_user(user.id)
    return [await _to_out(f, viewer_id=user.id, db=db) for f in friendships]


@router.post("/request", response_model=FriendOut, status_code=status.HTTP_201_CREATED)
async def request_friend(body: FriendRequestIn, user: CurrentUserDep, db: DbDep) -> FriendOut:
    target = await UserRepository(db).by_email(str(body.email))
    if target is None:
        raise NotFoundError("no account with that email")
    if target.id == user.id:
        raise ValidationError("you cannot befriend yourself")
    existing = await FriendshipRepository(db).find_pair(user.id, target.id)
    if existing is not None:
        raise ConflictError("a friendship or request already exists")
    created = await FriendshipRepository(db).add(requester_id=user.id, addressee_id=target.id)
    return await _to_out(created, viewer_id=user.id, db=db)


@router.post("/{friendship_id}/accept", response_model=FriendOut)
async def accept_friend(friendship_id: UUID, user: CurrentUserDep, db: DbDep) -> FriendOut:
    repo = FriendshipRepository(db)
    friendship = await repo.get(friendship_id)
    if friendship is None or friendship.addressee_id != user.id:
        raise NotFoundError("friend request not found")
    if friendship.status is FriendshipStatus.ACCEPTED:
        raise ConflictError("friend request already accepted")
    updated = await repo.set_status(friendship_id, FriendshipStatus.ACCEPTED)
    if updated is None:  # pragma: no cover - defensive
        raise NotFoundError("friend request not found")
    return await _to_out(updated, viewer_id=user.id, db=db)
