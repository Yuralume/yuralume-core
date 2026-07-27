"""Unit tests for the writer-side realtime outbox publisher (Phase 4 §7.1).

Drives the in-memory outbox so the publish→append→notify_hint contract and the
best-effort failure discipline are provable offline. The genuine
same-transaction rollback semantics (a shared SA session drops the event row on
rollback) live in the PostgreSQL integration suite — the publish sites here use
short-session appends after the domain rows already committed (see the module
docstring), so the unit level asserts the append/notify wiring and that a failed
commit yields no delivery signal.

Times are self-referential data anchored to a fixed base, never the wall clock.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.feed_event_bus import (
    FeedCommentReplyEvent,
    FeedEventBus,
    FeedPostEvent,
)
from kokoro_link.application.services.proactive_event_bus import (
    ProactiveEvent,
    ProactiveEventBus,
)
from kokoro_link.application.services.realtime_outbox_publisher import (
    OutboxFeedEventBus,
    OutboxProactiveEventBus,
    RealtimeOutboxPublisher,
    event_to_outbox_spec,
)
from kokoro_link.contracts.realtime_events import (
    EVENT_KIND_FEED_COMMENT_REPLY,
    EVENT_KIND_FEED_POST,
    EVENT_KIND_PROACTIVE,
)
from kokoro_link.infrastructure.repositories.in_memory_realtime_events import (
    InMemoryRealtimeOutbox,
)

BASE = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


def _proactive() -> ProactiveEvent:
    return ProactiveEvent(
        character_id="char-1",
        conversation_id="conv-1",
        message="想你了",
        created_at=BASE,
        unread_count=3,
    )


def _post() -> FeedPostEvent:
    return FeedPostEvent(
        character_id="char-1",
        post_id="post-9",
        kind="daily",
        content_text="今天去散步了",
        image_url="https://cdn.example/p.png",
        created_at=BASE,
    )


def _reply() -> FeedCommentReplyEvent:
    return FeedCommentReplyEvent(
        character_id="char-1",
        post_id="post-9",
        comment_id="cmt-2",
        content_text="謝謝你的留言",
        unread_count=5,
        created_at=BASE,
    )


# --- spec mapping: MINIMAL payload only (red line) ------------------------

def test_proactive_spec_carries_ids_and_count_never_message() -> None:
    spec = event_to_outbox_spec(_proactive())
    assert spec.event_kind == EVENT_KIND_PROACTIVE
    assert spec.payload == {
        "character_id": "char-1",
        "conversation_id": "conv-1",
        "unread_count": 3,
    }
    assert "message" not in spec.payload
    assert spec.created_at == BASE


def test_feed_post_spec_carries_ids_never_content_or_image() -> None:
    spec = event_to_outbox_spec(_post())
    assert spec.event_kind == EVENT_KIND_FEED_POST
    assert spec.payload == {"character_id": "char-1", "post_id": "post-9"}
    assert "content_text" not in spec.payload
    assert "image_url" not in spec.payload


def test_feed_comment_reply_spec_carries_ids_and_count_never_text() -> None:
    spec = event_to_outbox_spec(_reply())
    assert spec.event_kind == EVENT_KIND_FEED_COMMENT_REPLY
    assert spec.payload == {
        "character_id": "char-1",
        "post_id": "post-9",
        "comment_id": "cmt-2",
        "unread_count": 5,
    }
    assert "content_text" not in spec.payload


def test_unrelayable_type_raises() -> None:
    with pytest.raises(TypeError):
        event_to_outbox_spec(object())  # type: ignore[arg-type]


# --- publisher: append + post-commit notify -------------------------------

@pytest.mark.asyncio
async def test_publish_appends_and_fires_notify_with_row_id() -> None:
    outbox = InMemoryRealtimeOutbox()
    hints: list[int | None] = []

    async def _capture(*, event_id=None):
        hints.append(event_id)

    outbox.notify_hint = _capture  # type: ignore[assignment]
    publisher = RealtimeOutboxPublisher(outbox, session_factory=None)

    await publisher.publish(_proactive())

    rows = await outbox.read_after(0, limit=10)
    assert [r.event_kind for r in rows] == [EVENT_KIND_PROACTIVE]
    assert rows[0].payload["conversation_id"] == "conv-1"
    # Notify fired once, AFTER the append, carrying the stored row id.
    assert hints == [rows[0].id]


@pytest.mark.asyncio
async def test_publish_swallows_append_failure_and_skips_notify() -> None:
    class _BoomOutbox:
        def __init__(self) -> None:
            self.notified = False

        async def append(self, session, spec):
            raise RuntimeError("db down")

        async def notify_hint(self, *, event_id=None):
            self.notified = True

    outbox = _BoomOutbox()
    publisher = RealtimeOutboxPublisher(outbox, session_factory=None)
    # Best-effort: must not raise into the caller.
    await publisher.publish(_post())
    # A failed append means no delivery signal is emitted.
    assert outbox.notified is False


@pytest.mark.asyncio
async def test_publish_failed_commit_emits_no_notify() -> None:
    # Mirrors the "rollback leaves no event" red line at the unit seam: a session
    # whose commit fails must not fire the wake hint (no phantom delivery). The
    # append+rollback row-level proof is the PostgreSQL integration test.
    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            raise RuntimeError("commit failed")

    class _Outbox:
        def __init__(self) -> None:
            self.notified = False

        async def append(self, session, spec):
            from kokoro_link.contracts.realtime_events import StoredRealtimeEvent
            return StoredRealtimeEvent(
                id=1, event_kind=spec.event_kind,
                payload=dict(spec.payload), created_at=spec.created_at,
            )

        async def notify_hint(self, *, event_id=None):
            self.notified = True

    outbox = _Outbox()
    publisher = RealtimeOutboxPublisher(outbox, session_factory=_Session)
    await publisher.publish(_reply())
    assert outbox.notified is False


# --- decorator buses: local publish preserved + outbox hook ---------------

@pytest.mark.asyncio
async def test_outbox_proactive_bus_publishes_locally_and_appends() -> None:
    outbox = InMemoryRealtimeOutbox()
    publisher = RealtimeOutboxPublisher(outbox, session_factory=None)
    inner = ProactiveEventBus()
    bus = OutboxProactiveEventBus(inner, publisher)

    async with inner.subscription() as queue:
        await bus.publish(_proactive())
        # Local in-process delivery preserved (subscription surface delegates).
        assert queue.get_nowait().conversation_id == "conv-1"

    rows = await outbox.read_after(0, limit=10)
    assert [r.event_kind for r in rows] == [EVENT_KIND_PROACTIVE]


@pytest.mark.asyncio
async def test_outbox_feed_bus_publishes_locally_and_appends() -> None:
    outbox = InMemoryRealtimeOutbox()
    publisher = RealtimeOutboxPublisher(outbox, session_factory=None)
    inner = FeedEventBus()
    bus = OutboxFeedEventBus(inner, publisher)

    async with inner.subscription() as queue:
        await bus.publish(_post())
        await bus.publish(_reply())
        assert queue.get_nowait().post_id == "post-9"
        assert queue.get_nowait().comment_id == "cmt-2"

    rows = await outbox.read_after(0, limit=10)
    assert [r.event_kind for r in rows] == [
        EVENT_KIND_FEED_POST, EVENT_KIND_FEED_COMMENT_REPLY,
    ]


# --- reader mode: append only, dispatcher owns local delivery -------------

@pytest.mark.asyncio
async def test_reader_mode_appends_without_local_delivery() -> None:
    # The api role holds SSE connections AND runs a dispatcher over the same
    # outbox, so a local mirror here would deliver the event twice. Reader mode
    # appends only; the dispatcher is the single path to local subscribers.
    outbox = InMemoryRealtimeOutbox()
    publisher = RealtimeOutboxPublisher(outbox, session_factory=None)
    inner = FeedEventBus()
    bus = OutboxFeedEventBus(inner, publisher, local_publish=False)

    assert bus.mirrors_locally is False
    async with inner.subscription() as queue:
        await bus.publish(_post())
        assert queue.empty()

    assert [r.event_kind for r in await outbox.read_after(0, limit=10)] == [
        EVENT_KIND_FEED_POST,
    ]


@pytest.mark.asyncio
async def test_reader_mode_proactive_bus_appends_without_local_delivery() -> None:
    outbox = InMemoryRealtimeOutbox()
    publisher = RealtimeOutboxPublisher(outbox, session_factory=None)
    inner = ProactiveEventBus()
    bus = OutboxProactiveEventBus(inner, publisher, local_publish=False)

    assert bus.mirrors_locally is False
    async with inner.subscription() as queue:
        await bus.publish(_proactive())
        assert queue.empty()

    assert [r.event_kind for r in await outbox.read_after(0, limit=10)] == [
        EVENT_KIND_PROACTIVE,
    ]


def test_writer_mode_is_the_default() -> None:
    # Worker/background semantics must stay untouched by the reader-mode knob.
    publisher = RealtimeOutboxPublisher(InMemoryRealtimeOutbox(), None)
    assert OutboxFeedEventBus(FeedEventBus(), publisher).mirrors_locally is True
    assert OutboxProactiveEventBus(
        ProactiveEventBus(), publisher,
    ).mirrors_locally is True


# --- reader-mode fallback when the outbox is unreachable ------------------
#
# Reader mode has exactly ONE delivery path (its dispatcher tailing the outbox),
# so a swallowed append failure used to lose the event for every replica — a
# strict regression on the pre-outbox behaviour, where at least this process's
# own subscribers were served. ``publish`` therefore reports whether the event
# actually became durable, and reader mode degrades to local-only on False.


class _DeadOutbox:
    """Append always fails — the DB-down case."""

    def __init__(self) -> None:
        self.notified = False

    async def append(self, session, spec):
        raise RuntimeError("db down")

    async def notify_hint(self, *, event_id=None):
        self.notified = True


def _drain(queue) -> list:
    received = []
    while not queue.empty():
        received.append(queue.get_nowait())
    return received


@pytest.mark.asyncio
async def test_publish_reports_whether_the_event_became_durable() -> None:
    good = RealtimeOutboxPublisher(InMemoryRealtimeOutbox(), session_factory=None)
    dead = RealtimeOutboxPublisher(_DeadOutbox(), session_factory=None)

    assert await good.publish(_post()) is True
    assert await dead.publish(_post()) is False


@pytest.mark.asyncio
async def test_failed_notify_hint_still_reports_a_durable_append() -> None:
    # The row committed; only the wake nudge was lost, and the dispatcher's next
    # poll picks it up regardless. Reporting failure here would make reader mode
    # ALSO publish locally and its subscribers would see the event twice.
    outbox = InMemoryRealtimeOutbox()

    async def _boom(*, event_id=None):
        raise RuntimeError("notify channel down")

    outbox.notify_hint = _boom  # type: ignore[assignment]
    publisher = RealtimeOutboxPublisher(outbox, session_factory=None)

    assert await publisher.publish(_post()) is True
    assert len(await outbox.read_after(0, limit=10)) == 1


@pytest.mark.asyncio
async def test_reader_mode_falls_back_to_local_delivery_when_outbox_is_down() -> None:
    publisher = RealtimeOutboxPublisher(_DeadOutbox(), session_factory=None)
    inner = FeedEventBus()
    bus = OutboxFeedEventBus(inner, publisher, local_publish=False)

    async with inner.subscription() as queue:
        await bus.publish(_post())
        received = _drain(queue)

    # Exactly once: the dispatcher will never see a row that was never written.
    assert [event.post_id for event in received] == ["post-9"]


@pytest.mark.asyncio
async def test_reader_mode_proactive_falls_back_when_outbox_is_down() -> None:
    publisher = RealtimeOutboxPublisher(_DeadOutbox(), session_factory=None)
    inner = ProactiveEventBus()
    bus = OutboxProactiveEventBus(inner, publisher, local_publish=False)

    async with inner.subscription() as queue:
        await bus.publish(_proactive())
        received = _drain(queue)

    assert [event.conversation_id for event in received] == ["conv-1"]


@pytest.mark.asyncio
async def test_reader_mode_notify_failure_does_not_double_deliver() -> None:
    outbox = InMemoryRealtimeOutbox()

    async def _boom(*, event_id=None):
        raise RuntimeError("notify channel down")

    outbox.notify_hint = _boom  # type: ignore[assignment]
    publisher = RealtimeOutboxPublisher(outbox, session_factory=None)
    inner = FeedEventBus()
    bus = OutboxFeedEventBus(inner, publisher, local_publish=False)

    async with inner.subscription() as queue:
        await bus.publish(_post())
        # The row is durable, so the dispatcher owns delivery — no local mirror.
        assert _drain(queue) == []

    assert len(await outbox.read_after(0, limit=10)) == 1


@pytest.mark.asyncio
async def test_writer_mode_outbox_failure_does_not_double_publish() -> None:
    # Writer roles already mirror locally, so a dead outbox must change nothing.
    publisher = RealtimeOutboxPublisher(_DeadOutbox(), session_factory=None)
    inner = FeedEventBus()
    bus = OutboxFeedEventBus(inner, publisher, local_publish=True)

    async with inner.subscription() as queue:
        await bus.publish(_post())
        received = _drain(queue)

    assert [event.post_id for event in received] == ["post-9"]
