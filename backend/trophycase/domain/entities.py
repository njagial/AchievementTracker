"""Domain entities.

Framework-free value carriers used by the application layer. Infrastructure maps its
SQLModel tables to these types (``trophycase.infrastructure.db.mappers``) so business
logic never depends on SQLAlchemy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from trophycase.domain.rules import RuleSpec

__all__ = [
    "Achievement",
    "DeviceSession",
    "Friendship",
    "FriendshipStatus",
    "LeaderboardEntry",
    "ProgressLog",
    "SyncOperation",
    "User",
    "UserAchievement",
    "UserEvent",
    "UserStatus",
]


class UserStatus(StrEnum):
    ACTIVE = "active"
    SUSPENDED = "suspended"


class FriendshipStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"


@dataclass(frozen=True, slots=True)
class User:
    id: UUID
    email: str
    display_name: str
    status: UserStatus
    created_at: datetime
    updated_at: datetime
    total_score: int = 0
    avatar_url: str | None = None
    # Credentials stay on the aggregate: only the auth use case reads them and the HTTP
    # layer maps entities to explicit Pydantic schemas, so they can never leak by accident.
    password_hash: str | None = None
    oauth_provider: str | None = None
    oauth_subject: str | None = None


@dataclass(frozen=True, slots=True)
class Achievement:
    id: UUID
    slug: str
    title: str
    description: str
    icon: str
    points: int
    rule: RuleSpec
    is_active: bool
    created_at: datetime


@dataclass(frozen=True, slots=True)
class UserAchievement:
    id: UUID
    user_id: UUID
    achievement_id: UUID
    unlocked_at: datetime
    current: float
    target: float


@dataclass(frozen=True, slots=True)
class ProgressLog:
    """Persisted accumulator backing :class:`trophycase.domain.rules.ProgressState`."""

    user_id: UUID
    achievement_id: UUID
    count: int
    value: float
    streak: int
    last_day: date | None
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class DeviceSession:
    """One signed-in device; each holds its own refresh token."""

    id: UUID
    user_id: UUID
    device_id: str
    device_name: str
    refresh_token_hash: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Friendship:
    id: UUID
    requester_id: UUID
    addressee_id: UUID
    status: FriendshipStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class UserEvent:
    """Ingested user event (idempotent on ``id``)."""

    id: UUID
    user_id: UUID
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime
    received_at: datetime
    client_id: str | None = None
    seq: int | None = None


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    user_id: UUID
    display_name: str
    score: int
    rank: int
    unlocked_count: int = 0
    avatar_url: str | None = None


@dataclass(frozen=True, slots=True)
class SyncOperation:
    """Journal entry describing how one offline operation was reconciled."""

    op_id: UUID
    user_id: UUID
    client_id: str
    seq: int
    kind: str
    status: str
    conflict: bool
    response: dict[str, Any]
    created_at: datetime
