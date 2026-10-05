"""FastAPI application factory: routers, middleware, error mapping and lifespan."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text

from trophycase import __version__
from trophycase.config import get_settings
from trophycase.domain.errors import DomainError
from trophycase.infrastructure.db.session import dispose_engine, get_engine
from trophycase.infrastructure.event_bus import EventBus, close_redis
from trophycase.interfaces.api.middleware import RateLimitMiddleware
from trophycase.interfaces.api.routers import (
    achievements,
    auth,
    events,
    friends,
    leaderboards,
    oauth,
    sync,
    ws,
)

logger = logging.getLogger(__name__)

__all__ = ["app", "create_app"]


def _domain_error_response(exc: DomainError) -> JSONResponse:
    headers: dict[str, str] = {}
    if exc.code == "rate_limited":
        headers["Retry-After"] = str(exc.details.get("retry_after", 60))
    return JSONResponse(
        status_code=exc.http_status,
        content={
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
        headers=headers,
    )


async def _domain_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):  # pragma: no cover - guarded by handler key
        raise exc
    return _domain_error_response(exc)


async def _validation_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):  # pragma: no cover
        raise exc
    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "request body failed validation",
            "details": {"errors": exc.errors()},
        },
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    logging.basicConfig(
        level=logging.DEBUG if get_settings().debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        await EventBus().ensure_consumer_group()
    except Exception:  # Redis may not be up yet; the worker retries on its next tick
        logger.warning("could not create the event stream consumer group yet")
    yield
    await dispose_engine()
    await close_redis()
    logger.info("shutdown complete")


def create_app() -> FastAPI:
    """Build the ASGI application."""
    settings = get_settings()
    app = FastAPI(
        title="TrophyCase API",
        version=__version__,
        description="Real-time, offline-first, cross-device achievement tracker.",
        docs_url="/docs" if not settings.is_production or settings.debug else None,
        openapi_url="/openapi.json" if not settings.is_production or settings.debug else None,
        lifespan=lifespan,
    )
    app.add_exception_handler(DomainError, _domain_handler)
    app.add_exception_handler(RequestValidationError, _validation_handler)
    app.add_middleware(RateLimitMiddleware)

    for router in (
        auth.router,
        oauth.router,
        events.router,
        achievements.router,
        sync.router,
        leaderboards.router,
        friends.router,
        ws.router,
    ):
        app.include_router(router)

    # ``response_model=None``: the handler mixes a JSON body with a 503 JSONResponse.
    @app.get("/healthz", tags=["ops"], response_model=None)
    async def healthz() -> JSONResponse | dict[str, Any]:
        """Liveness/readiness probe: database + redis."""
        status: dict[str, Any] = {"api": "ok", "version": __version__}
        try:
            async with get_engine().connect() as conn:
                await conn.execute(text("SELECT 1"))
            status["database"] = "ok"
        except Exception:  # health checks must never raise
            status["database"] = "error"
        try:
            from trophycase.infrastructure.event_bus import get_redis

            await get_redis().ping()
            status["redis"] = "ok"
        except Exception:  # ignore: transport errors must not break the probe
            status["redis"] = "error"
        healthy = status["database"] == "ok" and status["redis"] == "ok"
        if not healthy:
            return JSONResponse(status_code=503, content=status)
        return status

    return app


app = create_app()


def run() -> None:
    """Console entry point (``trophycase-api``)."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "trophycase.main:app",
        host="0.0.0.0",  # noqa: S104 - the API runs behind Docker's published port
        port=8000,
        reload=settings.debug,
        log_level="debug" if settings.debug else "info",
    )
