"""PostgreSQL integration tests for the realtime dispatcher (Phase 4 §7.1).

Proves the behaviours only a real database exercises:

* two dispatchers (simulating two api replicas) each independently drain and
  rehydrate every event — cross-replica fan-out;
* a dropped ``NOTIFY`` is harmless — the fallback poll (``drain_once``) still
  delivers everything;
* the out-of-order-commit anti-loss window survives at the dispatcher level: a
  low-id row that commits after a reader passed a higher id is still delivered,
  and the higher id is not re-delivered (seen-id de-dup);
* a real ``LISTEN`` connection wakes the running dispatcher and it delivers.

Uses the proactive/conversation path because ``conversations`` has no character
foreign key, so a row can be seeded without the full character graph. Requires
Docker (testcontainers); skipped automatically when unavailable. Times are
self-referential data anchored to a fixed base, never the wall clock.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.proactive_event_bus import ProactiveEvent
from kokoro_link.application.services.realtime_event_dispatcher import (
    RealtimeEventDispatcher,
    RealtimeEventRehydrator,
)
from kokoro_link.contracts.realtime_events import (
    EVENT_KIND_PROACTIVE,
    REALTIME_EVENT_CHANNEL,
    RealtimeEventSpec,
)
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageRole,
)
from kokoro_link.infrastructure.persistence.sa_conversation_repository import (
    SAConversationRepository,
)
from kokoro_link.infrastructure.persistence.sa_feed_comment_repository import (
    SAFeedCommentRepository,
)
from kokoro_link.infrastructure.persistence.sa_feed_post_repository import (
    SAFeedPostRepository,
)
from kokoro_link.infrastructure.persistence.sa_realtime_events import (
    SARealtimeOutbox,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


def _rehydrator(session_factory) -> RealtimeEventRehydrator:
    return RealtimeEventRehydrator(
        SAConversationRepository(session_factory),
        SAFeedPostRepository(session_factory),
        SAFeedCommentRepository(session_factory),
    )


async def _seed_conversation(session_factory, *, conv_id, text, offset=0) -> None:
    repo = SAConversationRepository(session_factory)
    # Stamp the assistant message with the same clock the matching proactive
    # outbox event carries (BASE + offset). Production's proactive publisher does
    # this (``_deliver_web`` stamps the message with ``when``) so the rehydrator
    # can pin the exact message the event was appended for (sol #13).
    conversation = Conversation(
        id=conv_id, character_id="char-1",
        messages=[Message(
            role=MessageRole.ASSISTANT, content=text,
            created_at=BASE + timedelta(seconds=offset),
        )],
    )
    await repo.save(conversation)


async def _append_proactive(
    outbox, session_factory, *, conv_id, offset, unread, commit=True,
    session=None,
):
    spec = RealtimeEventSpec(
        event_kind=EVENT_KIND_PROACTIVE,
        payload={
            "character_id": "char-1",
            "conversation_id": conv_id,
            "unread_count": unread,
        },
        created_at=BASE + timedelta(seconds=offset),
    )
    if session is not None:
        return await outbox.append(session, spec)
    async with session_factory() as own:
        stored = await outbox.append(own, spec)
        if commit:
            await own.commit()
        return stored


async def test_two_replicas_each_receive_every_event(session_factory) -> None:
    outbox = SARealtimeOutbox(session_factory)
    await _seed_conversation(
        session_factory, conv_id="conv-1", text="訊息一", offset=0,
    )
    await _seed_conversation(
        session_factory, conv_id="conv-2", text="訊息二", offset=1,
    )
    await _append_proactive(
        outbox, session_factory, conv_id="conv-1", offset=0, unread=1,
    )
    await _append_proactive(
        outbox, session_factory, conv_id="conv-2", offset=1, unread=2,
    )

    bus_a, bus_b = _CapturingBus(), _CapturingBus()
    disp_a = RealtimeEventDispatcher(
        outbox, _rehydrator(session_factory), bus_a, None,
    )
    disp_b = RealtimeEventDispatcher(
        outbox, _rehydrator(session_factory), bus_b, None,
    )

    # NOTIFY dropped entirely — each replica's poll (drain_once) still delivers.
    assert await disp_a.drain_once() == 2
    assert await disp_b.drain_once() == 2
    for bus in (bus_a, bus_b):
        assert [e.conversation_id for e in bus.events] == ["conv-1", "conv-2"]
        assert all(isinstance(e, ProactiveEvent) for e in bus.events)
        assert all(e.event_id is not None for e in bus.events)
        # Message text was rehydrated from the durable row, not the outbox.
        assert bus.events[0].message == "訊息一"


async def test_out_of_order_commit_not_missed(session_factory) -> None:
    outbox = SARealtimeOutbox(session_factory)
    await _seed_conversation(
        session_factory, conv_id="conv-a", text="A文", offset=0,
    )
    await _seed_conversation(
        session_factory, conv_id="conv-b", text="B文", offset=1,
    )

    bus = _CapturingBus()
    disp = RealtimeEventDispatcher(
        outbox, _rehydrator(session_factory), bus, None,
    )

    async with session_factory() as sa:
        # A takes the LOWER id but stays uncommitted; B commits first.
        ev_a = await _append_proactive(
            outbox, session_factory, conv_id="conv-a", offset=0, unread=1,
            session=sa,
        )
        await _append_proactive(
            outbox, session_factory, conv_id="conv-b", offset=1, unread=2,
        )
        assert ev_a.id  # A owns a lower id than the committed B

        # First drain sees only committed B and advances the cursor past it.
        assert await disp.drain_once() == 1
        assert [e.conversation_id for e in bus.events] == ["conv-b"]

        await sa.commit()  # A commits LATE

    # Second drain: the overlap window re-surfaces the late low-id A; B is
    # re-read but de-duped by the seen set → A delivered exactly once.
    assert await disp.drain_once() == 1
    assert [e.conversation_id for e in bus.events] == ["conv-b", "conv-a"]


async def test_listen_wakes_running_dispatcher(
    session_factory, postgres_container,
) -> None:
    import asyncpg

    dsn = postgres_container.get_connection_url()
    for marker in ("+psycopg2", "+asyncpg"):
        dsn = dsn.replace(marker, "")

    class _AsyncpgListenSession:
        def __init__(self, conn: "asyncpg.Connection") -> None:
            self._conn = conn
            self._queue: asyncio.Queue[str] = asyncio.Queue()

        async def _bind(self) -> None:
            await self._conn.add_listener(
                REALTIME_EVENT_CHANNEL,
                lambda _c, _pid, _ch, payload: self._queue.put_nowait(payload),
            )

        async def wait(self) -> None:
            await self._queue.get()

        async def close(self) -> None:
            await self._conn.close()

    async def _listen_factory() -> _AsyncpgListenSession:
        conn = await asyncpg.connect(dsn)
        session = _AsyncpgListenSession(conn)
        await session._bind()
        return session

    outbox = SARealtimeOutbox(session_factory)
    await _seed_conversation(session_factory, conv_id="conv-live", text="即時")

    bus = _CapturingBus()
    disp = RealtimeEventDispatcher(
        outbox, _rehydrator(session_factory), bus, None,
        poll_interval=3600,  # only LISTEN can wake it in the test window
        prune_interval=0,
        listen_factory=_listen_factory,
    )
    await disp.start()
    try:
        # Append AFTER start (start primed the cursor past history), then fire
        # the NOTIFY that wakes the LISTEN loop → poll loop drains → bus gets it.
        await _append_proactive(
            outbox, session_factory, conv_id="conv-live", offset=0, unread=1,
        )
        await outbox.notify_hint(event_id=1)

        async def _wait_for_event() -> None:
            while not bus.events:
                await asyncio.sleep(0.02)

        await asyncio.wait_for(_wait_for_event(), timeout=5.0)
        assert bus.events[0].conversation_id == "conv-live"
        assert bus.events[0].message == "即時"
    finally:
        await disp.stop()
