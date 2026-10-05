"""Sliding-free fixed-window rate limiting backed by Redis.

Two layers are applied:

1. ``RateLimitMiddleware`` gives **every** client a per-minute budget.
2. ``limiter_dependency("login")`` puts a much smaller budget around credential
   endpoints, which is where brute-force attempts land.
"""

from __future__ import annotations

import time

from fastapi import Request
from redis.asyncio import Redis
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp

from trophycase.config import get_settings
from trophycase.domain.errors import RateLimitedError
from trophycase.infrastructure.event_bus import get_redis

__all__ = ["RateLimitMiddleware", "check_limit"]

_EXEMPT_PATHS = frozenset({"/healthz", "/metrics", "/docs", "/openapi.json", "/redoc"})


class RateLimiter:
    """Fixed-window counter: ``INCR`` + ``EXPIRE`` in a single pipeline."""

    def __init__(self, redis: Redis | None = None) -> None:
        self._redis: Redis = redis if redis is not None else get_redis()

    async def hit(self, key: str, *, limit: int, window_seconds: int = 60) -> tuple[bool, int, int]:
        """Record one hit; returns ``(allowed, remaining, retry_after_seconds)``."""
        pipe = self._redis.pipeline()
        pipe.incr(key)
        pipe.ttl(key)
        count_raw, ttl_raw = await pipe.execute()
        count = int(count_raw)
        ttl = int(ttl_raw)
        if ttl < 0:
            await self._redis.expire(key, window_seconds)
            ttl = window_seconds
        remaining = max(0, limit - count)
        return count <= limit, remaining, max(1, ttl)


def _client_key(request: Request, scope: str) -> str:
    client = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        client = forwarded.split(",")[0].strip()
    window = int(time.time() // 60)
    return f"ratelimit:{scope}:{client}:{window}"


async def check_limit(request: Request, *, scope: str, limit: int) -> None:
    """Raise :class:`RateLimitedError` when the budget is exhausted."""
    allowed, remaining, retry_after = await RateLimiter().hit(
        _client_key(request, scope), limit=limit
    )
    if not allowed:
        raise RateLimitedError(
            "too many requests", details={"retry_after": retry_after, "limit": limit}
        )
    request.state.rate_limit_remaining = remaining


def _too_many_requests(exc: RateLimitedError) -> JSONResponse:
    raw_retry = exc.details.get("retry_after", 60)
    retry_after = raw_retry if isinstance(raw_retry, int) else 60
    return JSONResponse(
        status_code=429,
        content={
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
        headers={"Retry-After": str(retry_after)},
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Per-IP budget for the whole API surface."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)
        self._limit = get_settings().rate_limit_per_minute

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)
        try:
            await check_limit(request, scope="api", limit=self._limit)
        except RateLimitedError as exc:
            return _too_many_requests(exc)
        response = await call_next(request)
        remaining = getattr(request.state, "rate_limit_remaining", None)
        if remaining is not None:
            response.headers["X-RateLimit-Limit"] = str(self._limit)
            response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response
