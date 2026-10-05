"""Repository implementations.

Thin data-access layer over SQLAlchemy 2.0 (async). Repositories return **domain
entities** - services never see a table class - and accept plain arguments, which keeps
the application layer free of SQL and makes fake repositories trivial in tests.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from trophycase.domain.entities import (
    Achievement,
    DeviceSession,
    Friendship,
    FriendshipStatus,
    ProgressLog,
    SyncOperation,
    User,
    UserAchievement,
    UserEvent,
)
from trophycase.infrastructure.db.mappers import (
    achievement_to_domain,
    device_session_to_domain,
    friendship_to_domain,
    progress_to_domain,
    sync_operation_to_domain,
    user_achievement_to_domain,
    user_event_to_domain,
    user_to_domain,
)
from trophycase.infrastructure.db.models import (
    AchievementTable,
    DeviceSessionTable,
    FriendshipTable,
    ProgressLogTable,
    SyncOperationTable,
    UserAchievementTable,
    UserEventTable,
    UserTable,
    utcnow,
)

__all__ = [
    "AchievementRepository",
    "DeviceSessionRepository",
    "EventRepository",
    "FriendshipRepository",
    "ProgressRepository",
    "SyncOperationRepository",
    "UserAchievementRepository",
    "UserRepository",
]


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UUID) -> User | None:
        row = await self._session.get(UserTable, user_id)
        return user_to_domain(row) if row is not None else None

    async def by_email(self, email: str) -> User | None:
        stmt = select(UserTable).where(func.lower(UserTable.email) == email.lower())
        row = (await self._session.execute(stmt)).scalars().first()
        return user_to_domain(row) if row is not None else None

    async def by_oauth(self, provider: str, subject: str) -> User | None:
        stmt = select(UserTable).where(
            UserTable.oauth_provider == provider,
            UserTable.oauth_subject == subject,
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return user_to_domain(row) if row is not None else None

    async def list_by_ids(self, ids: list[UUID]) -> list[User]:
        if not ids:
            return []
        stmt = select(UserTable).where(UserTable.id.in_(ids))
        rows = (await self._session.execute(stmt)).scalars().all()
        return [user_to_domain(row) for row in rows]

    async def top_scorers(self, *, limit: int = 100) -> list[User]:
        stmt = select(UserTable).order_by(UserTable.total_score.desc()).limit(limit)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [user_to_domain(row) for row in rows]

    async def add(self, user: User) -> User:
        row = UserTable(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            status=user.status.value,
            total_score=user.total_score,
            avatar_url=user.avatar_url,
            password_hash=user.password_hash,
            oauth_provider=user.oauth_provider,
            oauth_subject=user.oauth_subject,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )
        self._session.add(row)
        await self._session.flush()
        return user

    async def update_profile(
        self,
        user_id: UUID,
        *,
        display_name: str | None = None,
        avatar_url: str | None = None,
        password_hash: str | None = None,
    ) -> User | None:
        row = await self._session.get(UserTable, user_id)
        if row is None:
            return None
        if display_name is not None:
            row.display_name = display_name
        if avatar_url is not None:
            row.avatar_url = avatar_url
        if password_hash is not None:
            row.password_hash = password_hash
        row.updated_at = utcnow()
        await self._session.flush()
        return user_to_domain(row)

    async def set_score(self, user_id: UUID, score: int) -> None:
        row = await self._session.get(UserTable, user_id)
        if row is not None:
            row.total_score = max(0, score)
            row.updated_at = utcnow()
            await self._session.flush()


class DeviceSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def by_refresh_hash(self, token_hash: str) -> DeviceSession | None:
        stmt = select(DeviceSessionTable).where(DeviceSessionTable.refresh_token_hash == token_hash)
        row = (await self._session.execute(stmt)).scalars().first()
        return device_session_to_domain(row) if row is not None else None

    async def get(self, session_id: UUID) -> DeviceSession | None:
        row = await self._session.get(DeviceSessionTable, session_id)
        return device_session_to_domain(row) if row is not None else None

    async def by_user_device(self, user_id: UUID, device_id: str) -> DeviceSession | None:
        stmt = select(DeviceSessionTable).where(
            DeviceSessionTable.user_id == user_id,
            DeviceSessionTable.device_id == device_id,
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return device_session_to_domain(row) if row is not None else None

    async def list_for_user(
        self, user_id: UUID, *, active_only: bool = True
    ) -> list[DeviceSession]:
        stmt = select(DeviceSessionTable).where(DeviceSessionTable.user_id == user_id)
        if active_only:
            now = utcnow()
            stmt = stmt.where(
                DeviceSessionTable.revoked_at.is_(None), DeviceSessionTable.expires_at > now
            )
        stmt = stmt.order_by(DeviceSessionTable.last_seen_at.desc())
        rows = (await self._session.execute(stmt)).scalars().all()
        return [device_session_to_domain(row) for row in rows]

    async def add(self, session: DeviceSession) -> DeviceSession:
        row = DeviceSessionTable(
            id=session.id,
            user_id=session.user_id,
            device_id=session.device_id,
            device_name=session.device_name,
            refresh_token_hash=session.refresh_token_hash,
            created_at=session.created_at,
            last_seen_at=session.last_seen_at,
            expires_at=session.expires_at,
            revoked_at=session.revoked_at,
        )
        self._session.add(row)
        await self._session.flush()
        return session

    async def rotate(
        self, session_id: UUID, *, token_hash: str, expires_at: datetime
    ) -> DeviceSession | None:
        row = await self._session.get(DeviceSessionTable, session_id)
        if row is None:
            return None
        row.refresh_token_hash = token_hash
        row.expires_at = expires_at
        row.last_seen_at = utcnow()
        await self._session.flush()
        return device_session_to_domain(row)

    async def revoke(self, session_id: UUID) -> bool:
        row = await self._session.get(DeviceSessionTable, session_id)
        if row is None:
            return False
        row.revoked_at = utcnow()
        await self._session.flush()
        return True

    async def revoke_all_for_user(self, user_id: UUID) -> int:
        stmt = select(DeviceSessionTable).where(
            DeviceSessionTable.user_id == user_id,
            DeviceSessionTable.revoked_at.is_(None),
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        now = utcnow()
        for row in rows:
            row.revoked_at = now
        await self._session.flush()
        return len(rows)


class AchievementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, achievement_id: UUID) -> Achievement | None:
        row = await self._session.get(AchievementTable, achievement_id)
        return achievement_to_domain(row) if row is not None else None

    async def by_slug(self, slug: str) -> Achievement | None:
        stmt = select(AchievementTable).where(AchievementTable.slug == slug)
        row = (await self._session.execute(stmt)).scalars().first()
        return achievement_to_domain(row) if row is not None else None

    async def list_active(self) -> list[Achievement]:
        stmt = (
            select(AchievementTable)
            .where(AchievementTable.is_active.is_(True))
            .order_by(AchievementTable.created_at.asc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [achievement_to_domain(row) for row in rows]

    async def list_by_ids(self, ids: list[UUID]) -> list[Achievement]:
        if not ids:
            return []
        stmt = select(AchievementTable).where(AchievementTable.id.in_(ids))
        rows = (await self._session.execute(stmt)).scalars().all()
        return [achievement_to_domain(row) for row in rows]

    async def add(
        self,
        *,
        slug: str,
        title: str,
        description: str,
        icon: str,
        points: int,
        rule_spec: str,
        achievement_id: UUID | None = None,
    ) -> Achievement:
        kwargs: dict[str, Any] = {"id": achievement_id} if achievement_id is not None else {}
        row = AchievementTable(
            slug=slug,
            title=title,
            description=description,
            icon=icon,
            points=points,
            rule_spec=rule_spec,
            **kwargs,
        )
        self._session.add(row)
        await self._session.flush()
        return achievement_to_domain(row)


class ProgressRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UUID, achievement_id: UUID) -> ProgressLog | None:
        row = await self._session.get(
            ProgressLogTable, (user_id, achievement_id), with_for_update=True
        )
        return progress_to_domain(row) if row is not None else None

    async def list_for_user(self, user_id: UUID) -> list[ProgressLog]:
        stmt = select(ProgressLogTable).where(ProgressLogTable.user_id == user_id)
        rows = (await self._session.execute(stmt)).scalars().all()
        return [progress_to_domain(row) for row in rows]

    async def upsert(
        self,
        *,
        user_id: UUID,
        achievement_id: UUID,
        count: int,
        value: float,
        streak: int,
        last_day: date | None,
        updated_at: datetime,
    ) -> ProgressLog:
        """Insert or update the accumulator, falling back to update if we lost a race."""
        row = await self._session.get(
            ProgressLogTable, (user_id, achievement_id), with_for_update=True
        )
        if row is None:
            row = ProgressLogTable(
                user_id=user_id,
                achievement_id=achievement_id,
                count=count,
                value=value,
                streak=streak,
                last_day=last_day,
                updated_at=updated_at,
            )
            self._session.add(row)
            try:
                async with self._session.begin_nested():
                    await self._session.flush()
            except IntegrityError:
                row = await self._session.get(
                    ProgressLogTable, (user_id, achievement_id), with_for_update=True
                )
                if row is None:  # pragma: no cover - defensive
                    raise
                row.count = count
                row.value = value
                row.streak = streak
                row.last_day = last_day
                row.updated_at = updated_at
                await self._session.flush()
        else:
            row.count = count
            row.value = value
            row.streak = streak
            row.last_day = last_day
            row.updated_at = updated_at
            await self._session.flush()
        return ProgressLog(
            user_id=row.user_id,
            achievement_id=row.achievement_id,
            count=row.count,
            value=row.value,
            streak=row.streak,
            last_day=row.last_day,
            updated_at=row.updated_at,
        )


class UserAchievementRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, user_id: UUID, achievement_id: UUID) -> UserAchievement | None:
        stmt = select(UserAchievementTable).where(
            UserAchievementTable.user_id == user_id,
            UserAchievementTable.achievement_id == achievement_id,
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return user_achievement_to_domain(row) if row is not None else None

    async def list_for_user(self, user_id: UUID) -> list[UserAchievement]:
        stmt = (
            select(UserAchievementTable)
            .where(UserAchievementTable.user_id == user_id)
            .order_by(UserAchievementTable.unlocked_at.desc())
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [user_achievement_to_domain(row) for row in rows]

    async def count_for_user(self, user_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(UserAchievementTable)
            .where(UserAchievementTable.user_id == user_id)
        )
        return int((await self._session.execute(stmt)).scalar_one())

    async def add(
        self, *, user_id: UUID, achievement_id: UUID, current: float, target: float
    ) -> UserAchievement | None:
        row = UserAchievementTable(
            user_id=user_id,
            achievement_id=achievement_id,
            current=current,
            target=target,
        )
        self._session.add(row)
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError:
            return None  # already unlocked elsewhere
        return user_achievement_to_domain(row)


class EventRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def exists(self, event_id: UUID) -> bool:
        stmt = select(UserEventTable.id).where(UserEventTable.id == event_id)
        return (await self._session.execute(stmt)).first() is not None

    async def get(self, event_id: UUID) -> UserEvent | None:
        row = await self._session.get(UserEventTable, event_id)
        return user_event_to_domain(row) if row is not None else None

    async def is_processed(self, event_id: UUID) -> bool:
        stmt = select(UserEventTable.processed_at).where(UserEventTable.id == event_id)
        processed = (await self._session.execute(stmt)).scalar_one_or_none()
        return processed is not None

    async def add(self, event: UserEvent) -> bool:
        """Persist an event; returns ``False`` when the id was already recorded."""
        if await self.exists(event.id):
            return False
        row = UserEventTable(
            id=event.id,
            user_id=event.user_id,
            event_type=event.event_type,
            payload=json.dumps(event.payload, separators=(",", ":"), sort_keys=True),
            occurred_at=event.occurred_at,
            received_at=event.received_at,
            client_id=event.client_id,
            seq=event.seq,
        )
        self._session.add(row)
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError:
            return False
        return True

    async def list_since(
        self, user_id: UUID, since: datetime, *, limit: int = 200
    ) -> list[UserEvent]:
        stmt = (
            select(UserEventTable)
            .where(UserEventTable.user_id == user_id, UserEventTable.received_at > since)
            .order_by(UserEventTable.received_at.asc())
            .limit(limit)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [user_event_to_domain(row) for row in rows]

    async def mark_processed(self, event_ids: list[UUID], *, processed_at: datetime) -> int:
        if not event_ids:
            return 0
        stmt = (
            update(UserEventTable)
            .where(UserEventTable.id.in_(event_ids))
            .values(processed_at=processed_at)
        )
        result = await self._session.execute(stmt)
        return int(result.rowcount or 0)


class FriendshipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def friend_ids(self, user_id: UUID) -> set[UUID]:
        stmt = select(FriendshipTable.addressee_id).where(
            FriendshipTable.requester_id == user_id,
            FriendshipTable.status == FriendshipStatus.ACCEPTED.value,
        )
        friends = set((await self._session.execute(stmt)).scalars().all())
        stmt = select(FriendshipTable.requester_id).where(
            FriendshipTable.addressee_id == user_id,
            FriendshipTable.status == FriendshipStatus.ACCEPTED.value,
        )
        friends.update((await self._session.execute(stmt)).scalars().all())
        return friends

    async def list_for_user(self, user_id: UUID) -> list[Friendship]:
        stmt = select(FriendshipTable).where(
            (FriendshipTable.requester_id == user_id) | (FriendshipTable.addressee_id == user_id)
        )
        rows = (await self._session.execute(stmt)).scalars().all()
        return [friendship_to_domain(row) for row in rows]

    async def get(self, friendship_id: UUID) -> Friendship | None:
        row = await self._session.get(FriendshipTable, friendship_id)
        return friendship_to_domain(row) if row is not None else None

    async def find_pair(self, requester_id: UUID, addressee_id: UUID) -> Friendship | None:
        stmt = select(FriendshipTable).where(
            FriendshipTable.requester_id == requester_id,
            FriendshipTable.addressee_id == addressee_id,
        )
        row = (await self._session.execute(stmt)).scalars().first()
        return friendship_to_domain(row) if row is not None else None

    async def add(self, *, requester_id: UUID, addressee_id: UUID) -> Friendship:
        row = FriendshipTable(requester_id=requester_id, addressee_id=addressee_id)
        self._session.add(row)
        await self._session.flush()
        return friendship_to_domain(row)

    async def set_status(self, friendship_id: UUID, status: FriendshipStatus) -> Friendship | None:
        row = await self._session.get(FriendshipTable, friendship_id)
        if row is None:
            return None
        row.status = status.value
        row.updated_at = utcnow()
        await self._session.flush()
        return friendship_to_domain(row)


class SyncOperationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, op_id: UUID) -> SyncOperation | None:
        row = await self._session.get(SyncOperationTable, op_id)
        return sync_operation_to_domain(row) if row is not None else None

    async def add(
        self,
        *,
        op_id: UUID,
        user_id: UUID,
        client_id: str,
        seq: int,
        kind: str,
        status: str,
        conflict: bool,
        response: dict[str, Any],
    ) -> SyncOperation | None:
        row = SyncOperationTable(
            op_id=op_id,
            user_id=user_id,
            client_id=client_id,
            seq=seq,
            kind=kind,
            status=status,
            conflict=conflict,
            response=json.dumps(response, separators=(",", ":")),
        )
        self._session.add(row)
        try:
            async with self._session.begin_nested():
                await self._session.flush()
        except IntegrityError:
            return None
        return sync_operation_to_domain(row)
