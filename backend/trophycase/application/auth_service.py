"""Authentication use cases: registration, multi-device login, refresh rotation, OAuth2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from trophycase.config import get_settings
from trophycase.domain.entities import DeviceSession, User, UserStatus
from trophycase.domain.errors import ConflictError, UnauthorizedError, ValidationError
from trophycase.domain.ids import uuid7
from trophycase.infrastructure.db.models import utcnow
from trophycase.infrastructure.db.repositories import DeviceSessionRepository, UserRepository
from trophycase.infrastructure.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)

__all__ = ["AuthResult", "AuthService"]


@dataclass(frozen=True, slots=True)
class AuthResult:
    user: User
    session_id: UUID
    access_token: str
    refresh_token: str
    expires_in: int
    token_type: str = "bearer"


class AuthService:
    def __init__(self, session: AsyncSession) -> None:
        self._users = UserRepository(session)
        self._sessions = DeviceSessionRepository(session)

    # --- internals ---------------------------------------------------------------
    async def _open_session(
        self, user_id: UUID, *, device_id: str, device_name: str
    ) -> tuple[DeviceSession, str]:
        settings = get_settings()
        refresh_token = generate_refresh_token()
        token_hash = hash_token(refresh_token)
        expires_at = utcnow() + timedelta(seconds=settings.refresh_token_ttl_seconds)
        existing = await self._sessions.by_user_device(user_id, device_id)
        if existing is not None:
            rotated = await self._sessions.rotate(
                existing.id, token_hash=token_hash, expires_at=expires_at
            )
            if rotated is not None:
                return rotated, refresh_token
        session = DeviceSession(
            id=uuid7(),
            user_id=user_id,
            device_id=device_id,
            device_name=device_name,
            refresh_token_hash=token_hash,
            created_at=utcnow(),
            last_seen_at=utcnow(),
            expires_at=expires_at,
        )
        return await self._sessions.add(session), refresh_token

    async def _issue(self, user: User, *, device_id: str, device_name: str) -> AuthResult:
        session, refresh_token = await self._open_session(
            user.id, device_id=device_id, device_name=device_name
        )
        access_token = create_access_token(user_id=user.id, session_id=session.id)
        return AuthResult(
            user=user,
            session_id=session.id,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=get_settings().access_token_ttl_seconds,
        )

    @staticmethod
    def _require_active(user: User | None) -> User:
        if user is None:
            raise UnauthorizedError("invalid credentials")
        if user.status is not UserStatus.ACTIVE:
            raise UnauthorizedError("account is suspended")
        return user

    # --- use cases ---------------------------------------------------------------
    async def register(
        self,
        *,
        email: str,
        password: str,
        display_name: str,
        device_id: str = "default",
        device_name: str = "unknown device",
    ) -> AuthResult:
        if len(password) < 10:
            raise ValidationError("password must be at least 10 characters")
        if await self._users.by_email(email) is not None:
            raise ConflictError("an account with this email already exists")
        user = User(
            id=uuid7(),
            email=email.strip().lower(),
            display_name=display_name.strip(),
            status=UserStatus.ACTIVE,
            created_at=utcnow(),
            updated_at=utcnow(),
            password_hash=hash_password(password),
        )
        await self._users.add(user)
        return await self._issue(user, device_id=device_id, device_name=device_name)

    async def login(
        self,
        *,
        email: str,
        password: str,
        device_id: str = "default",
        device_name: str = "unknown device",
    ) -> AuthResult:
        user = await self._users.by_email(email)
        if user is None or user.password_hash is None:
            # Run a dummy verification so timing does not reveal whether the user exists.
            verify_password(password, hash_password("dummy-password-for-timing"))
            raise UnauthorizedError("invalid credentials")
        if not verify_password(password, user.password_hash):
            raise UnauthorizedError("invalid credentials")
        self._require_active(user)
        return await self._issue(user, device_id=device_id, device_name=device_name)

    async def refresh(
        self,
        *,
        refresh_token: str,
        device_id: str = "default",
        device_name: str = "unknown device",
    ) -> AuthResult:
        session = await self._sessions.by_refresh_hash(hash_token(refresh_token))
        if session is None or session.revoked_at is not None or session.expires_at <= utcnow():
            raise UnauthorizedError("refresh token is invalid or expired")
        user = await self._users.get(session.user_id)
        user = self._require_active(user)
        # Rotating here means a stolen refresh token can only be replayed once: the second
        # attempt finds a hash that no longer exists.
        return await self._issue(user, device_id=session.device_id, device_name=device_name)

    async def oauth_login(
        self,
        *,
        provider: str,
        subject: str,
        email: str,
        display_name: str,
        device_id: str = "default",
        device_name: str = "unknown device",
        avatar_url: str | None = None,
    ) -> AuthResult:
        user = await self._users.by_oauth(provider, subject)
        if user is None:
            existing = await self._users.by_email(email)
            if existing is not None:
                raise ConflictError("an account with this email already exists")
            user = User(
                id=uuid7(),
                email=email.strip().lower(),
                display_name=display_name.strip() or email.split("@")[0],
                status=UserStatus.ACTIVE,
                created_at=utcnow(),
                updated_at=utcnow(),
                avatar_url=avatar_url,
                oauth_provider=provider,
                oauth_subject=subject,
            )
            await self._users.add(user)
        self._require_active(user)
        return await self._issue(user, device_id=device_id, device_name=device_name)

    async def revoke_session(self, *, user_id: UUID, session_id: UUID) -> bool:
        session = await self._sessions.get(session_id)
        if session is None or session.user_id != user_id:
            return False
        return await self._sessions.revoke(session_id)

    async def revoke_all(self, *, user_id: UUID) -> int:
        return await self._sessions.revoke_all_for_user(user_id)

    async def list_sessions(self, user_id: UUID) -> list[DeviceSession]:
        return await self._sessions.list_for_user(user_id)

    async def get_user(self, user_id: UUID) -> User | None:
        return await self._users.get(user_id)
