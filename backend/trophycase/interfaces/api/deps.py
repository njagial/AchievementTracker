"""FastAPI dependencies: sessions, Redis and the authenticated request context."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated
from uuid import UUID

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from trophycase.config import Settings, get_settings
from trophycase.domain.entities import User
from trophycase.domain.errors import UnauthorizedError
from trophycase.infrastructure.db.models import utcnow
from trophycase.infrastructure.db.repositories import DeviceSessionRepository, UserRepository
from trophycase.infrastructure.db.session import get_session
from trophycase.infrastructure.event_bus import EventBus
from trophycase.infrastructure.security import decode_token
from trophycase.interfaces.api.middleware import check_limit

__all__ = [
    "AuthContext",
    "BusDep",
    "CurrentUserDep",
    "DbDep",
    "SettingsDep",
    "get_auth_context",
    "get_bus",
    "get_current_user",
    "get_settings_dep",
]

_bearer = HTTPBearer(auto_error=False, scheme_name="BearerAuth")


@dataclass(frozen=True, slots=True)
class AuthContext:
    """Authenticated identity for the current request."""

    user_id: UUID
    session_id: UUID


async def get_settings_dep() -> Settings:
    return get_settings()


def get_bus() -> EventBus:
    return EventBus()


async def get_auth_context(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: AsyncSession = Depends(get_session),
) -> AuthContext:
    """Validate the bearer access token **and** the underlying device session."""
    if credentials is None or not credentials.credentials:
        raise UnauthorizedError("missing bearer token")
    claims = decode_token(credentials.credentials, expected_type="access")
    session = await DeviceSessionRepository(db).get(claims.session_id)
    if session is None or session.revoked_at is not None or session.expires_at <= utcnow():
        raise UnauthorizedError("session has been revoked or expired")
    if session.user_id != claims.user_id:
        raise UnauthorizedError("token/session mismatch")
    return AuthContext(user_id=claims.user_id, session_id=claims.session_id)


async def get_current_user(
    context: Annotated[AuthContext, Depends(get_auth_context)],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    """Load the authenticated user, rejecting suspended accounts."""
    user = await UserRepository(db).get(context.user_id)
    if user is None:
        raise UnauthorizedError("account no longer exists")
    if user.status.value != "active":
        raise UnauthorizedError("account is suspended")
    return user


DbDep = Annotated[AsyncSession, Depends(get_session)]
SettingsDep = Annotated[Settings, Depends(get_settings_dep)]
BusDep = Annotated[EventBus, Depends(get_bus)]
CurrentUserDep = Annotated[User, Depends(get_current_user)]
AuthContextDep = Annotated[AuthContext, Depends(get_auth_context)]


def limiter_dependency(scope: str) -> Callable[..., Awaitable[None]]:
    """Build a route dependency that applies a per-IP budget (e.g. ``login``)."""

    async def _guard(request: Request) -> None:
        settings = get_settings()
        limit = (
            settings.rate_limit_login_per_minute
            if scope == "login"
            else settings.rate_limit_per_minute
        )
        await check_limit(request, scope=scope, limit=limit)

    return _guard
