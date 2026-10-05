"""Authentication endpoints: register, multi-device login, refresh, sessions."""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from trophycase.application.auth_service import AuthResult, AuthService
from trophycase.domain.entities import User
from trophycase.interfaces.api.deps import (
    AuthContextDep,
    CurrentUserDep,
    DbDep,
    limiter_dependency,
)
from trophycase.interfaces.api.schemas import (
    AuthOut,
    LoginIn,
    MessageOut,
    RefreshIn,
    RegisterIn,
    SessionOut,
    UserOut,
)

router = APIRouter(prefix="/v1/auth", tags=["auth"])

_login_limit = Annotated[None, Depends(limiter_dependency("login"))]


def to_auth_out(result: AuthResult) -> AuthOut:
    return AuthOut(
        access_token=result.access_token,
        refresh_token=result.refresh_token,
        token_type=result.token_type,
        expires_in=result.expires_in,
        session_id=result.session_id,
        user=UserOut(
            id=result.user.id,
            email=result.user.email,
            display_name=result.user.display_name,
            avatar_url=result.user.avatar_url,
            total_score=result.user.total_score,
            created_at=result.user.created_at,
            updated_at=result.user.updated_at,
        ),
    )


def to_user_out(user: User) -> UserOut:
    return UserOut(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        avatar_url=user.avatar_url,
        total_score=user.total_score,
        created_at=user.created_at,
        updated_at=user.updated_at,
    )


@router.post("/register", response_model=AuthOut, status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn, db: DbDep, _limit: _login_limit) -> AuthOut:
    service = AuthService(db)
    result = await service.register(
        email=str(body.email),
        password=body.password,
        display_name=body.display_name,
        device_id=body.device_id,
        device_name=body.device_name,
    )
    return to_auth_out(result)


@router.post("/login", response_model=AuthOut)
async def login(body: LoginIn, db: DbDep, _limit: _login_limit) -> AuthOut:
    service = AuthService(db)
    result = await service.login(
        email=str(body.email),
        password=body.password,
        device_id=body.device_id,
        device_name=body.device_name,
    )
    return to_auth_out(result)


@router.post("/refresh", response_model=AuthOut)
async def refresh(body: RefreshIn, db: DbDep, _limit: _login_limit) -> AuthOut:
    service = AuthService(db)
    result = await service.refresh(
        refresh_token=body.refresh_token,
        device_id=body.device_id,
        device_name=body.device_name,
    )
    return to_auth_out(result)


@router.get("/me", response_model=UserOut)
async def me(user: CurrentUserDep) -> UserOut:
    return to_user_out(user)


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(user: CurrentUserDep, db: DbDep) -> list[SessionOut]:
    sessions = await AuthService(db).list_sessions(user.id)
    return [
        SessionOut(
            id=s.id,
            device_id=s.device_id,
            device_name=s.device_name,
            created_at=s.created_at,
            last_seen_at=s.last_seen_at,
            expires_at=s.expires_at,
        )
        for s in sessions
    ]


@router.post("/logout", response_model=MessageOut)
async def logout(context: AuthContextDep, db: DbDep) -> MessageOut:
    await AuthService(db).revoke_session(user_id=context.user_id, session_id=context.session_id)
    return MessageOut(message="session revoked")


@router.delete("/sessions/{session_id}", response_model=MessageOut)
async def revoke_session(session_id: UUID, context: AuthContextDep, db: DbDep) -> MessageOut:
    revoked = await AuthService(db).revoke_session(user_id=context.user_id, session_id=session_id)
    if not revoked:
        from trophycase.domain.errors import NotFoundError

        raise NotFoundError("session not found")
    return MessageOut(message="session revoked")
