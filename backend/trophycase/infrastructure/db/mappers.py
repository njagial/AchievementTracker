"""Maps SQLModel rows to framework-free domain entities (and back)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

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
    UserStatus,
)
from trophycase.domain.rules import ProgressState, RuleSpec, parse_rule
from trophycase.infrastructure.db.models import (
    AchievementTable,
    DeviceSessionTable,
    FriendshipTable,
    ProgressLogTable,
    SyncOperationTable,
    UserAchievementTable,
    UserEventTable,
    UserTable,
)

__all__ = [
    "achievement_to_domain",
    "device_session_to_domain",
    "friendship_to_domain",
    "progress_to_domain",
    "progress_to_state",
    "rule_of",
    "sync_operation_to_domain",
    "user_achievement_to_domain",
    "user_event_to_domain",
    "user_to_domain",
]


def rule_of(row: AchievementTable) -> RuleSpec:
    """Parse the stored JSON rule spec into a typed rule."""
    return parse_rule(row.rule_spec)


def _utc(value: datetime) -> datetime:
    """Return an aware UTC timestamp.

    SQLite has no timezone type: it writes ``2026-01-01 12:00:00.000000+00:00`` but reads
    back a naive ``datetime``, while PostgreSQL returns an aware one. Normalising here
    keeps the domain layer identical on both backends.
    """
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value


def _utc_or_none(value: datetime | None) -> datetime | None:
    return None if value is None else _utc(value)


def user_to_domain(row: UserTable) -> User:
    return User(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        status=UserStatus(row.status),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
        total_score=row.total_score,
        avatar_url=row.avatar_url,
        password_hash=row.password_hash,
        oauth_provider=row.oauth_provider,
        oauth_subject=row.oauth_subject,
    )


def achievement_to_domain(row: AchievementTable) -> Achievement:
    return Achievement(
        id=row.id,
        slug=row.slug,
        title=row.title,
        description=row.description,
        icon=row.icon,
        points=row.points,
        rule=rule_of(row),
        is_active=row.is_active,
        created_at=_utc(row.created_at),
    )


def user_achievement_to_domain(row: UserAchievementTable) -> UserAchievement:
    return UserAchievement(
        id=row.id,
        user_id=row.user_id,
        achievement_id=row.achievement_id,
        unlocked_at=_utc(row.unlocked_at),
        current=row.current,
        target=row.target,
    )


def progress_to_state(row: ProgressLog | ProgressLogTable | None) -> ProgressState:
    if row is None:
        return ProgressState()
    return ProgressState(
        count=row.count,
        value=row.value,
        streak=row.streak,
        last_day=row.last_day,
    )


def progress_to_domain(row: ProgressLogTable) -> ProgressLog:
    return ProgressLog(
        user_id=row.user_id,
        achievement_id=row.achievement_id,
        count=row.count,
        value=row.value,
        streak=row.streak,
        last_day=row.last_day,
        updated_at=_utc(row.updated_at),
    )


def device_session_to_domain(row: DeviceSessionTable) -> DeviceSession:
    return DeviceSession(
        id=row.id,
        user_id=row.user_id,
        device_id=row.device_id,
        device_name=row.device_name,
        refresh_token_hash=row.refresh_token_hash,
        created_at=_utc(row.created_at),
        last_seen_at=_utc(row.last_seen_at),
        expires_at=_utc(row.expires_at),
        revoked_at=_utc_or_none(row.revoked_at),
    )


def friendship_to_domain(row: FriendshipTable) -> Friendship:
    return Friendship(
        id=row.id,
        requester_id=row.requester_id,
        addressee_id=row.addressee_id,
        status=FriendshipStatus(row.status),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def user_event_to_domain(row: UserEventTable) -> UserEvent:
    payload: dict[str, Any] = json.loads(row.payload or "{}")
    return UserEvent(
        id=row.id,
        user_id=row.user_id,
        event_type=row.event_type,
        payload=payload,
        occurred_at=_utc(row.occurred_at),
        received_at=_utc(row.received_at),
        client_id=row.client_id,
        seq=row.seq,
    )


def sync_operation_to_domain(row: SyncOperationTable) -> SyncOperation:
    response: dict[str, Any] = json.loads(row.response or "{}")
    return SyncOperation(
        op_id=row.op_id,
        user_id=row.user_id,
        client_id=row.client_id,
        seq=row.seq,
        kind=row.kind,
        status=row.status,
        conflict=row.conflict,
        response=response,
        created_at=_utc(row.created_at),
    )
