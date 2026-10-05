"""Event worker: consumes the Redis stream, evaluates rules and broadcasts results.

Delivery contract
-----------------
* **At-least-once.** A message is acknowledged only after its event row is marked
  processed, so a crash between evaluation and ``XACK`` replays the message safely:
  the worker short-circuits on ``processed_at`` instead of double counting.
* Notifications are published *after* the database transaction commits, so clients can
  never observe an unlock that did not happen.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from uuid import UUID

from trophycase.application.achievement_service import AchievementService
from trophycase.application.leaderboard_service import LeaderboardService
from trophycase.config import get_settings
from trophycase.infrastructure.db.models import utcnow
from trophycase.infrastructure.db.repositories import EventRepository
from trophycase.infrastructure.db.session import session_scope
from trophycase.infrastructure.event_bus import EventBus, close_redis

logger = logging.getLogger(__name__)

__all__ = ["EventWorker", "main"]


class EventWorker:
    def __init__(self, *, consumer: str | None = None, bus: EventBus | None = None) -> None:
        self._bus = bus if bus is not None else EventBus()
        self._consumer = consumer or f"worker-{os.getpid()}"

    async def handle_message(self, message_id: str, fields: dict[str, str]) -> None:
        try:
            event_id = UUID(str(fields["event_id"]))
        except (KeyError, ValueError):
            logger.warning("malformed stream entry %s: %r", message_id, fields)
            await self._bus.ack(message_id)
            return

        messages: list[dict[str, Any]] = []
        user_id: UUID | None = None
        score: int | None = None
        already_processed = False

        async with session_scope() as db:
            events = EventRepository(db)
            if await events.is_processed(event_id):
                already_processed = True
            else:
                event = await events.get(event_id)
                if event is None:
                    # Published before its row was visible (or the row never landed).
                    # Leave the message un-acknowledged: claim_stale() will offer it
                    # again, and the next pass sees the committed row.
                    logger.warning("event %s not visible yet; leaving it for retry", event_id)
                    return
                user_id = event.user_id
                report = await AchievementService(db).evaluate_event(event)
                messages = report.messages
                score = report.score
                await events.mark_processed([event_id], processed_at=utcnow())
                # Everything below (cache, ack, fan-out) must happen once the write is durable.
                await db.commit()
                if score is not None:
                    await LeaderboardService(db).sync_score(event.user_id, score)

        await self._bus.ack(message_id)
        if already_processed:
            return
        if user_id is None:
            return
        for message in messages:
            await self._bus.publish_user(user_id, message)

    async def _process(self, entries: list[tuple[str, dict[str, str]]]) -> None:
        for message_id, fields in entries:
            try:
                await self.handle_message(message_id, fields)
            except Exception:
                # Leave un-acknowledged: claim_stale() will retry it on a later tick.
                logger.exception("failed to process stream message %s", message_id)

    async def run_forever(self) -> None:
        settings = get_settings()
        logger.info("worker %s starting (stream=%s)", self._consumer, settings.redis_stream_key)
        await self._bus.ensure_consumer_group()
        while True:
            try:
                stale = await self._bus.claim_stale(self._consumer)
                if stale:
                    logger.info("reclaimed %d stale message(s)", len(stale))
                    await self._process(stale)
                entries = await self._bus.read_events(self._consumer, count=32, block_ms=2000)
                if entries:
                    await self._process(entries)
            except asyncio.CancelledError:  # pragma: no cover - shutdown path
                logger.info("worker cancelled")
                raise
            except Exception:
                logger.exception("worker loop error; retrying")
                await asyncio.sleep(1.0)

    async def drain(self) -> None:
        """Process everything currently in the stream once (used by tests)."""
        await self._bus.ensure_consumer_group()
        entries = await self._bus.read_events(self._consumer, count=256, block_ms=1)
        await self._process(entries)


def main() -> None:
    """Console entry point (``trophycase-worker``)."""
    logging.basicConfig(
        level=logging.DEBUG if get_settings().debug else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    async def _run() -> None:
        worker = EventWorker()
        try:
            await worker.run_forever()
        finally:
            await close_redis()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:  # pragma: no cover - interactive shutdown
        logger.info("worker stopped")


if __name__ == "__main__":  # pragma: no cover
    main()
