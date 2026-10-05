"""Password hashing, JWT issue/verify and opaque refresh-token helpers.

Security posture
----------------
* Passwords: Argon2id (``pwdlib`` recommended parameters).
* Access tokens: short-lived signed JWTs (``sub`` = user id, ``sid`` = device session).
* Refresh tokens: opaque random strings; only their SHA-256 hash is persisted, so a
  database leak cannot be replayed as a session.
* Nothing here ever weakens verification (no ``verify=False``, no ``algorithms=[None]``).
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Literal
from uuid import UUID, uuid4

import jwt
from pwdlib import PasswordHash

from trophycase.config import get_settings
from trophycase.domain.errors import UnauthorizedError

__all__ = [
    "TokenClaims",
    "create_access_token",
    "decode_token",
    "generate_refresh_token",
    "hash_password",
    "hash_token",
    "verify_password",
]

_password_hash = PasswordHash.recommended()

TokenType = Literal["access", "refresh"]


@dataclass(frozen=True, slots=True)
class TokenClaims:
    user_id: UUID
    session_id: UUID
    token_type: TokenType
    issued_at: int
    expires_at: int
    jti: str


def hash_password(password: str) -> str:
    """Hash a plaintext password with Argon2id."""
    return _password_hash.hash(password)


def verify_password(password: str, hashed: str) -> bool:
    """Constant-time password verification."""
    try:
        return _password_hash.verify(password, hashed)
    except ValueError:
        return False


def create_access_token(
    *,
    user_id: UUID,
    session_id: UUID,
    ttl_seconds: int | None = None,
    token_type: TokenType = "access",
) -> str:
    """Sign a compact JWT for the given user/device-session pair."""
    settings = get_settings()
    now = int(time.time())
    ttl = ttl_seconds if ttl_seconds is not None else settings.access_token_ttl_seconds
    payload: dict[str, object] = {
        "sub": str(user_id),
        "sid": str(session_id),
        "type": token_type,
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid4()),
    }
    return jwt.encode(payload, settings.secret_key.get_secret_value(), algorithm=settings.algorithm)


def decode_token(token: str, *, expected_type: TokenType = "access") -> TokenClaims:
    """Verify a JWT and return its claims, raising :class:`UnauthorizedError` on failure."""
    settings = get_settings()
    try:
        raw = jwt.decode(
            token,
            settings.secret_key.get_secret_value(),
            algorithms=[settings.algorithm],
            options={"require": ["exp", "iat", "sub", "sid", "type", "jti"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnauthorizedError("token has expired") from exc
    except jwt.PyJWTError as exc:
        raise UnauthorizedError("invalid token") from exc

    token_type = raw.get("type")
    if token_type != expected_type:
        raise UnauthorizedError(f"expected a {expected_type} token")
    try:
        return TokenClaims(
            user_id=UUID(str(raw["sub"])),
            session_id=UUID(str(raw["sid"])),
            token_type=expected_type,
            issued_at=int(raw["iat"]),
            expires_at=int(raw["exp"]),
            jti=str(raw["jti"]),
        )
    except (KeyError, ValueError, TypeError) as exc:  # pragma: no cover - guarded by require
        raise UnauthorizedError("malformed token claims") from exc


def generate_refresh_token() -> str:
    """Return a 256-bit opaque refresh token."""
    return secrets.token_urlsafe(48)


def hash_token(token: str) -> str:
    """SHA-256 (hex) of an opaque token - what gets stored in ``device_sessions``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
