"""Worker delivery contract: acknowledge work that happened, retry work that did not."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest_asyncio

from trophycase.application.auth_service import AuthService
from trophycase.domain.entities import UserEvent
from trophycase.infrastructure.db.repositories import EventRepository
from trophycase.infrastructure.db.session import get_session_factory
from trophycase.infrastructure.event_bus import EventBus
from trophycase.worker.main import EventWorker


class _SpyBus(EventBus):
    """Records acknowledgements so a test can prove what was (not) handed back."""

    def __init__(self) -> None:
        super().__init__()
        self.acked: list[str] = []

    async def ack(self, message_id: str) -> int:
        self.acked.append(message_id)
        return await super().ack(message_id)


@pytest_asyncio.fixture
async def user_id(db: None, redis: Any) -> AsyncGenerator[UUID]:
    """A registered account, committed and visible to the worker."""
    async with get_session_factory()() as session:
        auth = await AuthService(session).register(
            email=f"{uuid4().hex[:12]}@example.com",
            password="correct horse battery",
            display_name="Worker Tester",
            device_id="dev-1",
            device_name="pytest",
        )
        await session.commit()
        yield auth.user.id


async def test_an_event_that_has_not_landed_yet_is_left_for_retry(db: None, redis: Any) -> None:
    """Dropping it would lose the user's progress; it must stay in the pending list."""
    bus = _SpyBus()
    worker = EventWorker(consumer="tester", bus=bus)
    await bus.ensure_consumer_group()
    event_id = uuid4()
    message_id = await bus.push_event(
        event_id=event_id, user_id=uuid4(), event_type="task.completed"
    )

    await worker.handle_message(message_id, {"event_id": str(event_id)})

    assert bus.acked == []


async def test_a_visible_event_is_evaluated_and_acknowledged(
    db: None, redis: Any, user_id: UUID
) -> None:
    bus = _SpyBus()
    worker = EventWorker(consumer="tester", bus=bus)
    await bus.ensure_consumer_group()
    event_id = uuid4()
    async with get_session_factory()() as session:
        inserted = await EventRepository(session).add(
            UserEvent(
                id=event_id,
                user_id=user_id,
                event_type="task.completed",
                payload={"task_id": "t-1"},
                occurred_at=datetime.now(UTC),
                received_at=datetime.now(UTC),
                client_id="dev-1",
                seq=1,
            )
        )
        await session.commit()
    assert inserted is True
    message_id = await bus.push_event(
        event_id=event_id, user_id=user_id, event_type="task.completed"
    )

    await worker.handle_message(message_id, {"event_id": str(event_id)})

    assert bus.acked == [message_id]


async def test_a_processed_event_is_never_evaluated_twice(
    db: None, redis: Any, user_id: UUID
) -> None:
    """At-least-once delivery must not double count when a message is redelivered."""
    bus = _SpyBus()
    worker = EventWorker(consumer="tester", bus=bus)
    await bus.ensure_consumer_group()
    event_id = uuid4()
    async with get_session_factory()() as session:
        await EventRepository(session).add(
            UserEvent(
                id=event_id,
                user_id=user_id,
                event_type="task.completed",
                payload={},
                occurred_at=datetime.now(UTC),
                received_at=datetime.now(UTC),
                client_id="dev-1",
                seq=1,
            )
        )
        await session.commit()
    message_id = await bus.push_event(
        event_id=event_id, user_id=user_id, event_type="task.completed"
    )

    await worker.handle_message(message_id, {"event_id": str(event_id)})
    await worker.handle_message(message_id, {"event_id": str(event_id)})

    assert bus.acked == [message_id, message_id]
    async with get_session_factory()() as session:
        assert await EventRepository(session).is_processed(event_id) is True
