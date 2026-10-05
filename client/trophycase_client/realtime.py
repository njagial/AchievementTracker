"""Realtime channel: a reconnecting WebSocket that forwards server pushes to the app."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from trophycase_client.config import ClientConfig

__all__ = ["RealtimeClient"]

logger = logging.getLogger(__name__)

MessageHandler = Callable[[dict[str, Any]], Awaitable[None]]

_PING_INTERVAL = 20.0
_MAX_BACKOFF = 30.0


class RealtimeClient:
    """Keeps one WebSocket open for the lifetime of the app, reconnecting forever."""

    def __init__(
        self,
        config: ClientConfig,
        *,
        token: Callable[[], str | None],
        on_message: MessageHandler,
    ) -> None:
        self._config = config
        self._token = token
        self._on_message = on_message
        self._stopped = asyncio.Event()

    def stop(self) -> None:
        self._stopped.set()

    def _uri(self, access_token: str) -> str:
        parsed = urlparse(self._config.ws_url)
        query = urlencode({"token": access_token})
        return urlunparse(parsed._replace(query=query))

    async def run(self) -> None:
        """Connect, pump messages, reconnect with exponential backoff."""
        backoff = 1.0
        while not self._stopped.is_set():
            token = self._token()
            if not token:
                await asyncio.sleep(backoff)
                continue
            uri = self._uri(token)
            try:
                async with connect(
                    uri,
                    ssl=_ssl_context(uri, self._config.verify),
                    open_timeout=self._config.request_timeout_seconds,
                    max_size=2**20,
                ) as socket:
                    backoff = 1.0
                    await self._pump(socket)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("realtime disconnected: %s", exc)
            if self._stopped.is_set():
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, _MAX_BACKOFF)

    async def _pump(self, socket: Any) -> None:
        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(_PING_INTERVAL)
                await socket.send("ping")

        beat = asyncio.create_task(heartbeat())
        try:
            async for raw in socket:
                if self._stopped.is_set():
                    break
                if not isinstance(raw, str):
                    continue
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                await self._on_message(message)
        except ConnectionClosed:
            pass
        finally:
            beat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await beat


def _ssl_context(uri: str, verify: bool | str) -> ssl.SSLContext | None:
    """``None`` for plaintext ``ws://``; a *verifying* context for ``wss://``.

    ``create_default_context`` never turns verification off - the only knob is which CA
    bundle to trust, which is exactly the behaviour TrophyCase requires.
    """
    if urlparse(uri).scheme != "wss":
        return None
    return ssl.create_default_context(cafile=verify if isinstance(verify, str) else None)
