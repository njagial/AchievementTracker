"""Offline-first synchronisation: replay a device's outbox and hand back server state.

Reconciliation rules
--------------------
``event``   Append-only. Idempotent on ``op_id`` (the event id), so a device that replays
            its whole outbox after a crash cannot double-count progress.
``profile`` Last-writer-wins. The client sends ``base_updated_at`` (the server timestamp it
            last saw); if the server moved on, the operation is rejected with
            ``conflict=True`` and the authoritative snapshot is returned.

Progress counters are server-authoritative and monotonic - they never go backwards, so a
stale device only ever converges forward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from trophycase.domain.entities import UserEvent
from trophycase.domain.errors import ValidationError
from trophycase.infrastructure.db.repositories import (
    EventRepository,
    SyncOperationRepository,
    UserRepository,
)
from trophycase.infrastructure.event_bus import EventBus

__all__ = ["SyncOp", "SyncOpResult", "SyncPullResult", "SyncPushResult", "SyncService"]

Status = Literal["applied", "duplicate", "rejected"]


@dataclass(frozen=True, slots=True)
class SyncOp:
    """One operation replayed from a device outbox."""

    op_id: UUID
    seq: int
    kind: str
    event_type: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: datetime | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    base_updated_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class SyncOpResult:
    op_id: UUID
    status: Status
    conflict: bool
    detail: dict[str, Any]


@dataclass(frozen=True, slots=True)
class SyncPushResult:
    results: list[SyncOpResult]
    server_time: datetime

    @property
    def applied(self) -> int:
        return sum(1 for r in self.results if r.status == "applied")

    @property
    def duplicates(self) -> int:
        return sum(1 for r in self.results if r.status == "duplicate")

    @property
    def conflicts(self) -> int:
        return sum(1 for r in self.results if r.conflict)


@dataclass(frozen=True, slots=True)
class SyncPullResult:
    cursor: datetime
    server_time: datetime
    events: list[UserEvent]
    has_more: bool


class SyncService:
    def __init__(self, session: AsyncSession, bus: EventBus | None = None) -> None:
        self._session = session
        self._events = EventRepository(session)
        self._ops = SyncOperationRepository(session)
        self._users = UserRepository(session)
        self._bus = bus if bus is not None else EventBus()

    async def _apply_event(self, op: SyncOp, *, user_id: UUID) -> SyncOpResult:
        if op.event_type is None or op.occurred_at is None:
            raise ValidationError("event operations require event_type and occurred_at")
        event = UserEvent(
            id=op.op_id,
            user_id=user_id,
            event_type=op.event_type,
            payload=op.payload,
            occurred_at=op.occurred_at,
            received_at=datetime.now(UTC),
            client_id=None,
            seq=op.seq,
        )
        inserted = await self._events.add(event)
        detail: dict[str, Any] = {"event_id": str(op.op_id)}
        if inserted:
            # The worker consumes the id immediately, so the row must be committed first.
            await self._session.commit()
            await self._bus.push_event(event_id=op.op_id, user_id=user_id, event_type=op.event_type)
            return SyncOpResult(op_id=op.op_id, status="applied", conflict=False, detail=detail)
        detail["reason"] = "already recorded"
        return SyncOpResult(op_id=op.op_id, status="duplicate", conflict=False, detail=detail)

    async def _apply_profile(self, op: SyncOp, *, user_id: UUID) -> SyncOpResult:
        user = await self._users.get(user_id)
        if user is None:
            return SyncOpResult(
                op_id=op.op_id, status="rejected", conflict=False, detail={"reason": "unknown user"}
            )
        base = op.base_updated_at
        if base is not None and base.tzinfo is None:
            base = base.replace(tzinfo=UTC)  # a client without an offset means UTC
        if base is not None and user.updated_at > base:
            return SyncOpResult(
                op_id=op.op_id,
                status="rejected",
                conflict=True,
                detail={
                    "reason": "stale_base_timestamp",
                    "server": {
                        "display_name": user.display_name,
                        "avatar_url": user.avatar_url,
                        "updated_at": user.updated_at.isoformat(),
                    },
                },
            )
        display_name = op.fields.get("display_name")
        avatar_url = op.fields.get("avatar_url")
        updated = await self._users.update_profile(
            user_id,
            display_name=str(display_name) if display_name is not None else None,
            avatar_url=str(avatar_url) if avatar_url is not None else None,
        )
        return SyncOpResult(
            op_id=op.op_id,
            status="applied",
            conflict=False,
            detail={
                "display_name": updated.display_name if updated else None,
                "avatar_url": updated.avatar_url if updated else None,
                "updated_at": updated.updated_at.isoformat() if updated else None,
            },
        )

    async def push(self, *, user_id: UUID, client_id: str, ops: list[SyncOp]) -> SyncPushResult:
        """Replay an ordered batch of offline operations."""
        results: list[SyncOpResult] = []
        for op in ops:
            recorded = await self._ops.get(op.op_id)
            if recorded is not None:
                results.append(
                    SyncOpResult(
                        op_id=op.op_id,
                        status="duplicate",
                        conflict=recorded.conflict,
                        detail=recorded.response,
                    )
                )
                continue
            if op.kind == "event":
                outcome = await self._apply_event(op, user_id=user_id)
            elif op.kind == "profile":
                outcome = await self._apply_profile(op, user_id=user_id)
            else:  # pragma: no cover - Literal guard
                outcome = SyncOpResult(
                    op_id=op.op_id,
                    status="rejected",
                    conflict=False,
                    detail={"reason": f"unsupported kind {op.kind!r}"},
                )
            await self._ops.add(
                op_id=op.op_id,
                user_id=user_id,
                client_id=client_id,
                seq=op.seq,
                kind=op.kind,
                status=outcome.status,
                conflict=outcome.conflict,
                response=outcome.detail,
            )
            results.append(outcome)
        return SyncPushResult(results=results, server_time=datetime.now(UTC))

    async def pull(
        self, *, user_id: UUID, since: datetime | None = None, limit: int = 100
    ) -> SyncPullResult:
        """Return events the client has not seen yet plus an opaque cursor."""
        cursor = since if since is not None else datetime(1970, 1, 1, tzinfo=UTC)
        if cursor.tzinfo is None:
            cursor = cursor.replace(tzinfo=UTC)
        events = await self._events.list_since(user_id, cursor, limit=limit)
        has_more = len(events) >= limit
        new_cursor = events[-1].received_at if events else cursor
        return SyncPullResult(
            cursor=new_cursor,
            server_time=datetime.now(UTC),
            events=events,
            has_more=has_more,
        )
