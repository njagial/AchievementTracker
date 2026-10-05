"""Event ingestion use case: validate, persist (idempotent) and enqueue for evaluation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from trophycase.domain.entities import UserEvent
from trophycase.domain.errors import ValidationError
from trophycase.domain.ids import uuid7
from trophycase.infrastructure.db.models import utcnow
from trophycase.infrastructure.db.repositories import EventRepository
from trophycase.infrastructure.event_bus import EventBus

__all__ = ["EventService", "IngestResult"]

_EVENT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_.]{0,63}$")
_MAX_PAYLOAD_BYTES = 8_192
_MAX_FUTURE_SKEW = timedelta(hours=24)


@dataclass(frozen=True, slots=True)
class IngestResult:
    event_id: UUID
    duplicate: bool


class EventService:
    def __init__(self, session: AsyncSession, bus: EventBus | None = None) -> None:
        self._session = session
        self._events = EventRepository(session)
        self._bus = bus if bus is not None else EventBus()

    @staticmethod
    def validate(event_type: str, payload: dict[str, Any], occurred_at: datetime) -> None:
        if not _EVENT_TYPE_RE.match(event_type):
            raise ValidationError(
                "event_type must be snake_case (a-z, 0-9, '_', '.'), max 64 chars"
            )
        encoded = json.dumps(payload, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
            raise ValidationError(f"payload exceeds {_MAX_PAYLOAD_BYTES} bytes")
        if occurred_at > datetime.now(UTC) + _MAX_FUTURE_SKEW:
            raise ValidationError("occurred_at is too far in the future")

    async def ingest(
        self,
        *,
        user_id: UUID,
        event_type: str,
        payload: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
        client_id: str | None = None,
        seq: int | None = None,
        event_id: UUID | None = None,
    ) -> IngestResult:
        """Persist an event and push it onto the evaluation stream.

        Ingestion is idempotent on ``event_id``: replays from the offline outbox return
        ``duplicate=True`` without creating a second row. A duplicate that has not been
        evaluated yet is re-enqueued so a lost stream message can be recovered.
        """
        body = payload or {}
        when = occurred_at or datetime.now(UTC)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        self.validate(event_type, body, when)

        event = UserEvent(
            id=event_id if event_id is not None else uuid7(),
            user_id=user_id,
            event_type=event_type,
            payload=body,
            occurred_at=when,
            received_at=utcnow(),
            client_id=client_id,
            seq=seq,
        )

        inserted = await self._events.add(event)
        if not inserted and await self._events.is_processed(event.id):
            return IngestResult(event_id=event.id, duplicate=True)

        # Commit before the stream message that names this row goes out: the worker reads
        # the id straight away and would otherwise race the request's own transaction.
        await self._session.commit()
        await self._bus.push_event(event_id=event.id, user_id=user_id, event_type=event_type)
        return IngestResult(event_id=event.id, duplicate=not inserted)
