"""SQLModel table definitions.

Design notes
------------
* Every timestamp is ``DateTime(timezone=True)`` so PostgreSQL uses ``TIMESTAMPTZ`` and
  SQLite keeps the offset - values are always written as UTC.
* Rule specs and event payloads are stored as JSON **text**: portable across PostgreSQL
  and SQLite (tests), and parsed into typed Pydantic models at the boundary.
* Primary keys are UUIDv7 (time sortable) except progress logs, which key on the
  ``(user_id, achievement_id)`` pair because there is exactly one accumulator per pair.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import DateTime, Text, UniqueConstraint
from sqlmodel import Field, SQLModel

from trophycase.domain.ids import uuid7

__all__ = [
    "AchievementTable",
    "DeviceSessionTable",
    "FriendshipTable",
    "ProgressLogTable",
    "SyncOperationTable",
    "UserAchievementTable",
    "UserEventTable",
    "UserTable",
    "utcnow",
]

TIMESTAMP = DateTime(timezone=True)


def utcnow() -> datetime:
    """Timezone-aware 'now' in UTC."""
    return datetime.now(UTC)


class UserTable(SQLModel, table=True):
    __tablename__ = "users"
    __table_args__ = (UniqueConstraint("oauth_provider", "oauth_subject", name="uq_oauth_subject"),)

    id: UUID = Field(primary_key=True, default_factory=uuid7)
    email: str = Field(max_length=320, unique=True, index=True)
    display_name: str = Field(max_length=80, index=True)
    password_hash: str | None = Field(default=None, max_length=512)
    status: str = Field(default="active", max_length=16, index=True)
    total_score: int = Field(default=0, index=True)
    avatar_url: str | None = Field(default=None, max_length=512)
    oauth_provider: str | None = Field(default=None, max_length=32)
    oauth_subject: str | None = Field(default=None, max_length=255)
    created_at: datetime = Field(default_factory=utcnow, sa_type=TIMESTAMP)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=TIMESTAMP)


class AchievementTable(SQLModel, table=True):
    __tablename__ = "achievements"

    id: UUID = Field(primary_key=True, default_factory=uuid7)
    slug: str = Field(max_length=64, unique=True, index=True)
    title: str = Field(max_length=120)
    description: str = Field(default="", max_length=500)
    icon: str = Field(default="trophy", max_length=64)
    points: int = Field(default=10, ge=0, le=1_000_000)
    rule_spec: str = Field(sa_type=Text)
    is_active: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=utcnow, sa_type=TIMESTAMP)


class UserAchievementTable(SQLModel, table=True):
    __tablename__ = "user_achievements"
    __table_args__ = (UniqueConstraint("user_id", "achievement_id", name="uq_user_achievement"),)

    id: UUID = Field(primary_key=True, default_factory=uuid7)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    achievement_id: UUID = Field(foreign_key="achievements.id", index=True)
    unlocked_at: datetime = Field(default_factory=utcnow, sa_type=TIMESTAMP)
    current: float = Field(default=0.0)
    target: float = Field(default=0.0)


class ProgressLogTable(SQLModel, table=True):
    __tablename__ = "progress_logs"

    user_id: UUID = Field(primary_key=True, foreign_key="users.id")
    achievement_id: UUID = Field(primary_key=True, foreign_key="achievements.id")
    count: int = Field(default=0, ge=0)
    value: float = Field(default=0.0)
    streak: int = Field(default=0, ge=0)
    last_day: date | None = Field(default=None)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=TIMESTAMP)


class DeviceSessionTable(SQLModel, table=True):
    __tablename__ = "device_sessions"
    __table_args__ = (UniqueConstraint("user_id", "device_id", name="uq_user_device"),)

    id: UUID = Field(primary_key=True, default_factory=uuid7)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    device_id: str = Field(max_length=64)
    device_name: str = Field(default="unknown device", max_length=120)
    refresh_token_hash: str = Field(max_length=128, unique=True, index=True)
    created_at: datetime = Field(default_factory=utcnow, sa_type=TIMESTAMP)
    last_seen_at: datetime = Field(default_factory=utcnow, sa_type=TIMESTAMP)
    expires_at: datetime = Field(sa_type=TIMESTAMP)
    revoked_at: datetime | None = Field(default=None, sa_type=TIMESTAMP)


class FriendshipTable(SQLModel, table=True):
    __tablename__ = "friendships"
    __table_args__ = (UniqueConstraint("requester_id", "addressee_id", name="uq_friendship"),)

    id: UUID = Field(primary_key=True, default_factory=uuid7)
    requester_id: UUID = Field(foreign_key="users.id", index=True)
    addressee_id: UUID = Field(foreign_key="users.id", index=True)
    status: str = Field(default="pending", max_length=16, index=True)
    created_at: datetime = Field(default_factory=utcnow, sa_type=TIMESTAMP)
    updated_at: datetime = Field(default_factory=utcnow, sa_type=TIMESTAMP)


class UserEventTable(SQLModel, table=True):
    """Idempotent event log; ``id`` is generated client-side so replays are free."""

    __tablename__ = "user_events"
    __table_args__ = (UniqueConstraint("user_id", "seq", name="uq_user_seq"),)

    id: UUID = Field(primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    event_type: str = Field(max_length=64, index=True)
    payload: str = Field(sa_type=Text, default="{}")
    occurred_at: datetime = Field(sa_type=TIMESTAMP)
    received_at: datetime = Field(default_factory=utcnow, sa_type=TIMESTAMP)
    client_id: str | None = Field(default=None, max_length=64, index=True)
    seq: int | None = Field(default=None)
    processed_at: datetime | None = Field(default=None, sa_type=TIMESTAMP)


class SyncOperationTable(SQLModel, table=True):
    """Journal of applied sync operations - the dedupe + audit log for offline replays."""

    __tablename__ = "sync_operations"

    op_id: UUID = Field(primary_key=True)
    user_id: UUID = Field(foreign_key="users.id", index=True)
    client_id: str = Field(max_length=64, index=True)
    seq: int = Field(default=0, ge=0)
    kind: str = Field(default="event", max_length=32)
    status: str = Field(default="applied", max_length=16)
    conflict: bool = Field(default=False)
    response: str = Field(default="{}", sa_type=Text)
    created_at: datetime = Field(default_factory=utcnow, sa_type=TIMESTAMP)
