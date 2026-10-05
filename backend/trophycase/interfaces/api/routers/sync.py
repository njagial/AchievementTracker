"""Offline synchronisation endpoints: replay an outbox, then pull what changed."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query

from trophycase.application.sync_service import SyncOp, SyncService
from trophycase.interfaces.api.deps import BusDep, CurrentUserDep, DbDep
from trophycase.interfaces.api.schemas import (
    EventRecordOut,
    SyncPullOut,
    SyncPushIn,
    SyncPushOut,
    SyncResultOut,
)

router = APIRouter(prefix="/v1/sync", tags=["sync"])


@router.post("/batch", response_model=SyncPushOut)
async def push_batch(body: SyncPushIn, user: CurrentUserDep, db: DbDep, bus: BusDep) -> SyncPushOut:
    ops = [
        SyncOp(
            op_id=item.op_id,
            seq=item.seq,
            kind=item.kind,
            event_type=item.event_type,
            payload=item.payload,
            occurred_at=item.occurred_at,
            fields=item.fields,
            base_updated_at=item.base_updated_at,
        )
        for item in body.ops
    ]
    outcome = await SyncService(db, bus).push(user_id=user.id, client_id=body.client_id, ops=ops)
    return SyncPushOut(
        results=[
            SyncResultOut(
                op_id=result.op_id,
                status=result.status,
                conflict=result.conflict,
                detail=result.detail,
            )
            for result in outcome.results
        ],
        applied=outcome.applied,
        duplicates=outcome.duplicates,
        conflicts=outcome.conflicts,
        server_time=outcome.server_time,
    )


@router.get("/changes", response_model=SyncPullOut)
async def pull_changes(
    user: CurrentUserDep,
    db: DbDep,
    bus: BusDep,
    since: datetime | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> SyncPullOut:
    outcome = await SyncService(db, bus).pull(user_id=user.id, since=since, limit=limit)
    return SyncPullOut(
        cursor=outcome.cursor,
        server_time=outcome.server_time,
        has_more=outcome.has_more,
        events=[
            EventRecordOut(
                id=event.id,
                event_type=event.event_type,
                payload=event.payload,
                occurred_at=event.occurred_at,
                received_at=event.received_at,
            )
            for event in outcome.events
        ],
    )
