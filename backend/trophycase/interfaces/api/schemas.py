"""Transport schemas (Pydantic v2) for every REST endpoint.

Domain entities never cross the wire directly: each response is an explicit schema, which
keeps internal fields (password hashes, token digests) from leaking by accident.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

__all__ = [
    "AchievementOut",
    "AuthOut",
    "CatalogueOut",
    "ErrorOut",
    "EventBatchIn",
    "EventBatchOut",
    "EventIn",
    "EventOut",
    "EventRecordOut",
    "FriendOut",
    "FriendRequestIn",
    "LeaderboardEntryOut",
    "LeaderboardOut",
    "LoginIn",
    "MessageOut",
    "RefreshIn",
    "RegisterIn",
    "SessionOut",
    "SyncOpIn",
    "SyncPullOut",
    "SyncPushIn",
    "SyncPushOut",
    "SyncResultOut",
    "UserOut",
]


class _Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# --- auth ---------------------------------------------------------------------------


class RegisterIn(_Schema):
    email: EmailStr
    password: str = Field(min_length=10, max_length=256)
    display_name: str = Field(min_length=1, max_length=80)
    device_id: str = Field(default="default", min_length=1, max_length=64)
    device_name: str = Field(default="unknown device", min_length=1, max_length=120)


class LoginIn(_Schema):
    email: EmailStr
    password: str = Field(min_length=1, max_length=256)
    device_id: str = Field(default="default", min_length=1, max_length=64)
    device_name: str = Field(default="unknown device", min_length=1, max_length=120)


class RefreshIn(_Schema):
    refresh_token: str = Field(min_length=20, max_length=512)
    device_id: str = Field(default="default", min_length=1, max_length=64)
    device_name: str = Field(default="unknown device", min_length=1, max_length=120)


class UserOut(_Schema):
    id: UUID
    email: EmailStr
    display_name: str
    avatar_url: str | None = None
    total_score: int
    created_at: datetime
    #: Server timestamp the client must echo back as ``base_updated_at`` when it pushes a
    #: profile write - it is what makes last-writer-wins conflict detection possible.
    updated_at: datetime


class AuthOut(_Schema):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    session_id: UUID
    user: UserOut


class SessionOut(_Schema):
    id: UUID
    device_id: str
    device_name: str
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime


# --- events -------------------------------------------------------------------------


class EventIn(_Schema):
    event_type: str = Field(min_length=1, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None
    client_id: str | None = Field(default=None, max_length=64)
    seq: int | None = Field(default=None, ge=0)
    event_id: UUID | None = None


class EventBatchIn(_Schema):
    events: list[EventIn] = Field(min_length=1, max_length=200)


class EventOut(_Schema):
    event_id: UUID
    duplicate: bool
    accepted: int = 1


class EventBatchOut(_Schema):
    results: list[EventOut]
    accepted: int


class EventRecordOut(_Schema):
    id: UUID
    event_type: str
    payload: dict[str, Any]
    occurred_at: datetime
    received_at: datetime


# --- achievements -------------------------------------------------------------------


class AchievementOut(_Schema):
    id: UUID
    slug: str
    title: str
    description: str
    icon: str
    points: int
    current: float
    target: float
    progress: float = Field(ge=0, le=1)
    unlocked: bool
    unlocked_at: datetime | None = None
    #: The declarative rule spec. Returned so an offline client can fold events into
    #: progress with the very same engine the server uses (optimistic UI).
    rule: dict[str, Any] = Field(default_factory=dict)
    #: Raw accumulator (``count``/``value``/``streak``/``last_day``) so a reconnecting
    #: client resumes offline evaluation exactly where the server left off.
    state: dict[str, Any] = Field(default_factory=dict)


class CatalogueOut(_Schema):
    achievements: list[AchievementOut]
    unlocked: int
    score: int
    generated_at: datetime


class AchievementCreateIn(_Schema):
    slug: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    icon: str = Field(default="trophy", max_length=64)
    points: int = Field(default=10, ge=0, le=1_000_000)
    rule: dict[str, Any]


# --- leaderboards / friends ---------------------------------------------------------


class LeaderboardEntryOut(_Schema):
    rank: int
    user_id: UUID
    display_name: str
    score: int
    avatar_url: str | None = None
    you: bool = False


class LeaderboardOut(_Schema):
    scope: Literal["global", "friends"]
    entries: list[LeaderboardEntryOut]
    generated_at: datetime


class FriendRequestIn(_Schema):
    email: EmailStr


class FriendOut(_Schema):
    id: UUID
    user_id: UUID
    display_name: str
    status: str


# --- sync ---------------------------------------------------------------------------


class SyncOpIn(_Schema):
    op_id: UUID
    seq: int = Field(ge=0)
    kind: Literal["event", "profile"] = "event"
    event_type: str | None = Field(default=None, max_length=64)
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    base_updated_at: datetime | None = None

    @field_validator("kind")
    @classmethod
    def _known_kind(cls, value: str) -> str:
        if value not in {"event", "profile"}:
            raise ValueError(f"unsupported sync kind {value!r}")
        return value


class SyncPushIn(_Schema):
    client_id: str = Field(min_length=1, max_length=64)
    ops: list[SyncOpIn] = Field(min_length=1, max_length=500)


class SyncResultOut(_Schema):
    op_id: UUID
    status: Literal["applied", "duplicate", "rejected"]
    conflict: bool
    detail: dict[str, Any] = Field(default_factory=dict)


class SyncPushOut(_Schema):
    results: list[SyncResultOut]
    applied: int
    duplicates: int
    conflicts: int
    server_time: datetime


class SyncPullOut(_Schema):
    cursor: datetime
    server_time: datetime
    has_more: bool
    events: list[EventRecordOut]


# --- misc ---------------------------------------------------------------------------


class MessageOut(_Schema):
    message: str


class ErrorOut(_Schema):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
