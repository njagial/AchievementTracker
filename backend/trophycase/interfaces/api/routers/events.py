"""Event ingestion endpoints (single + batch)."""

from __future__ import annotations

from fastapi import APIRouter, status

from trophycase.application.event_service import EventService
from trophycase.interfaces.api.deps import BusDep, CurrentUserDep, DbDep
from trophycase.interfaces.api.schemas import EventBatchIn, EventBatchOut, EventIn, EventOut

router = APIRouter(prefix="/v1/events", tags=["events"])


@router.post("", response_model=EventOut, status_code=status.HTTP_202_ACCEPTED)
async def ingest_event(body: EventIn, user: CurrentUserDep, db: DbDep, bus: BusDep) -> EventOut:
    service = EventService(db, bus)
    result = await service.ingest(
        user_id=user.id,
        event_type=body.event_type,
        payload=body.payload,
        occurred_at=body.occurred_at,
        client_id=body.client_id,
        seq=body.seq,
        event_id=body.event_id,
    )
    return EventOut(event_id=result.event_id, duplicate=result.duplicate)


@router.post("/batch", response_model=EventBatchOut, status_code=status.HTTP_202_ACCEPTED)
async def ingest_batch(
    body: EventBatchIn, user: CurrentUserDep, db: DbDep, bus: BusDep
) -> EventBatchOut:
    service = EventService(db, bus)
    results: list[EventOut] = []
    for item in body.events:
        outcome = await service.ingest(
            user_id=user.id,
            event_type=item.event_type,
            payload=item.payload,
            occurred_at=item.occurred_at,
            client_id=item.client_id,
            seq=item.seq,
            event_id=item.event_id,
        )
        results.append(EventOut(event_id=outcome.event_id, duplicate=outcome.duplicate))
    return EventBatchOut(
        results=results,
        accepted=sum(1 for r in results if not r.duplicate),
    )
