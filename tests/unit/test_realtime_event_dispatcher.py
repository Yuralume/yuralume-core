"""Unit tests for the api-side realtime outbox dispatcher (Phase 4 §7.1).

Drives the in-memory outbox + fake domain readers + capturing buses so the
drain / rehydrate / cursor / seen-eviction logic is provable offline (no DB, no
LISTEN — poll-free ``drain_once``). Times are self-referential data anchored to
a fixed base, never the wall clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kokoro_link.application.services.feed_event_bus import (
    FeedCommentReplyEvent,
    FeedPostEvent,
)
from kokoro_link.application.services.proactive_event_bus import ProactiveEvent
from kokoro_link.application.services.realtime_event_dispatcher import (
    RealtimeEventDispatcher,
    RealtimeEventRehydrator,
)
from kokoro_link.contracts.realtime_events import (
    EVENT_KIND_FEED_COMMENT_REPLY,
    EVENT_KIND_FEED_POST,
    EVENT_KIND_PROACTIVE,
    RealtimeEventSpec,
)
from kokoro_link.domain.entities.conversation import MessageRole
from kokoro_link.infrastructure.repositories.in_memory_realtime_events import (
    InMemoryRealtimeOutbox,
)

BASE = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


class _CapturingBus:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def publish(self, event: object) -> None:
        self.events.append(event)


class _DictReader:
    def __init__(self, rows: dict[str, object]) -> None:
        self._rows = rows

    async def get(self, key: str) -> object | None:
        return self._rows.get(key)


def _conversation(text: str, *, created_at: datetime = BASE):
    # The proactive publisher stamps the assistant message with the same clock
    # the outbox row carries; the rehydrator pins on that stamp. Default the
    # assistant turn to BASE so an ``offset=0`` append matches it.
    return SimpleNamespace(messages=[
        SimpleNamespace(
            role=MessageRole.USER, content="hi",
            created_at=created_at - timedelta(seconds=1),
        ),
        SimpleNamespace(
            role=MessageRole.ASSISTANT, content=text, created_at=created_at,
        ),
    ])


def _post_row():
    return SimpleNamespace(
        id="post-9", character_id="char-1", kind=SimpleNamespace(value="daily"),
        content_text="散步中", image_url="https://cdn/p.png", created_at=BASE,
    )


def _comment_row():
    return SimpleNamespace(
        id="cmt-2", post_id="post-9", content_text="謝謝", created_at=BASE,
    )


def _build(*, conversations=None, posts=None, comments=None):
    rehydrator = RealtimeEventRehydrator(
        _DictReader(conversations or {}),
        _DictReader(posts or {}),
        _DictReader(comments or {}),
    )
    return rehydrator


async def _append(outbox, *, kind, offset, **payload):
    return await outbox.append(
        None,
        RealtimeEventSpec(
            event_kind=kind, payload=payload,
            created_at=BASE + timedelta(seconds=offset),
        ),
    )


# --- rehydrator -----------------------------------------------------------

@pytest.mark.asyncio
async def test_rehydrate_proactive_reads_message_text() -> None:
    outbox = InMemoryRealtimeOutbox()
    stored = await _append(
        outbox, kind=EVENT_KIND_PROACTIVE, offset=0,
        character_id="char-1", conversation_id="conv-1", unread_count=3,
    )
    reh = _build(conversations={"conv-1": _conversation("在想你")})
    event = await reh.rehydrate(stored)
    assert isinstance(event, ProactiveEvent)
    assert event.message == "在想你"
    assert event.unread_count == 3
    assert event.event_id == stored.id
    assert event.created_at == BASE


@pytest.mark.asyncio
async def test_rehydrate_proactive_pins_exact_message_not_latest() -> None:
    # Regression (sol #13): the player completes a NEW round between the outbox
    # append and this rehydrate, so the conversation's newest assistant message
    # is the fresh reply — not the proactive text. The rehydrator must return the
    # message stamped at the event's created_at, never "the latest assistant".
    outbox = InMemoryRealtimeOutbox()
    stored = await _append(
        outbox, kind=EVENT_KIND_PROACTIVE, offset=0,
        character_id="char-1", conversation_id="conv-1", unread_count=2,
    )  # stored.created_at == BASE
    conversation = SimpleNamespace(messages=[
        SimpleNamespace(
            role=MessageRole.USER, content="早", created_at=BASE - timedelta(seconds=1),
        ),
        # The proactive message the event was appended for (stamped at BASE).
        SimpleNamespace(
            role=MessageRole.ASSISTANT, content="想你了", created_at=BASE,
        ),
        # A newer round the player produced AFTER the append but BEFORE rehydrate.
        SimpleNamespace(
            role=MessageRole.USER, content="我在", created_at=BASE + timedelta(seconds=30),
        ),
        SimpleNamespace(
            role=MessageRole.ASSISTANT, content="嗯嗯剛剛在忙",
            created_at=BASE + timedelta(seconds=31),
        ),
    ])
    reh = _build(conversations={"conv-1": conversation})
    event = await reh.rehydrate(stored)
    assert isinstance(event, ProactiveEvent)
    assert event.message == "想你了"  # the pinned message, NOT the newer reply


@pytest.mark.asyncio
async def test_rehydrate_proactive_skips_when_message_gone() -> None:
    # No assistant message matches the event's stamp (edited / pruned) → skip,
    # rather than deliver an unrelated turn's text.
    outbox = InMemoryRealtimeOutbox()
    stored = await _append(
        outbox, kind=EVENT_KIND_PROACTIVE, offset=0,
        character_id="char-1", conversation_id="conv-1", unread_count=1,
    )
    conversation = SimpleNamespace(messages=[
        SimpleNamespace(
            role=MessageRole.ASSISTANT, content="別的訊息",
            created_at=BASE + timedelta(seconds=99),
        ),
    ])
    reh = _build(conversations={"conv-1": conversation})
    assert await reh.rehydrate(stored) is None


@pytest.mark.asyncio
async def test_rehydrate_feed_post_reads_content() -> None:
    outbox = InMemoryRealtimeOutbox()
    stored = await _append(
        outbox, kind=EVENT_KIND_FEED_POST, offset=0,
        character_id="char-1", post_id="post-9",
    )
    reh = _build(posts={"post-9": _post_row()})
    event = await reh.rehydrate(stored)
    assert isinstance(event, FeedPostEvent)
    assert event.content_text == "散步中"
    assert event.image_url == "https://cdn/p.png"
    assert event.event_id == stored.id


@pytest.mark.asyncio
async def test_rehydrate_feed_comment_reply_reads_text() -> None:
    outbox = InMemoryRealtimeOutbox()
    stored = await _append(
        outbox, kind=EVENT_KIND_FEED_COMMENT_REPLY, offset=0,
        character_id="char-1", post_id="post-9", comment_id="cmt-2",
        unread_count=5,
    )
    reh = _build(comments={"cmt-2": _comment_row()})
    event = await reh.rehydrate(stored)
    assert isinstance(event, FeedCommentReplyEvent)
    assert event.content_text == "謝謝"
    assert event.unread_count == 5
    assert event.event_id == stored.id


@pytest.mark.asyncio
async def test_rehydrate_miss_returns_none() -> None:
    outbox = InMemoryRealtimeOutbox()
    stored = await _append(
        outbox, kind=EVENT_KIND_FEED_POST, offset=0,
        character_id="char-1", post_id="gone",
    )
    reh = _build(posts={})  # row deleted
    assert await reh.rehydrate(stored) is None


@pytest.mark.asyncio
async def test_rehydrate_unknown_kind_returns_none() -> None:
    outbox = InMemoryRealtimeOutbox()
    stored = await _append(outbox, kind="mystery", offset=0, x="y")
    reh = _build()
    assert await reh.rehydrate(stored) is None


# --- dispatcher drain -----------------------------------------------------

@pytest.mark.asyncio
async def test_drain_dispatches_by_kind_and_advances_cursor() -> None:
    outbox = InMemoryRealtimeOutbox()
    await _append(
        outbox, kind=EVENT_KIND_PROACTIVE, offset=0,
        character_id="char-1", conversation_id="conv-1", unread_count=1,
    )
    await _append(
        outbox, kind=EVENT_KIND_FEED_POST, offset=1,
        character_id="char-1", post_id="post-9",
    )
    reh = _build(
        conversations={"conv-1": _conversation("hi")},
        posts={"post-9": _post_row()},
    )
    proactive, feed = _CapturingBus(), _CapturingBus()
    disp = RealtimeEventDispatcher(outbox, reh, proactive, feed)

    published = await disp.drain_once()
    assert published == 2
    assert len(proactive.events) == 1
    assert len(feed.events) == 1
    # A second drain with no new rows publishes nothing (cursor advanced).
    assert await disp.drain_once() == 0
    assert len(proactive.events) == 1 and len(feed.events) == 1


@pytest.mark.asyncio
async def test_drain_dedupes_overlap_re_read() -> None:
    outbox = InMemoryRealtimeOutbox()
    for i in range(3):
        await _append(
            outbox, kind=EVENT_KIND_FEED_POST, offset=i,
            character_id="char-1", post_id="post-9",
        )
    reh = _build(posts={"post-9": _post_row()})
    feed = _CapturingBus()
    disp = RealtimeEventDispatcher(outbox, reh, None, feed)

    assert await disp.drain_once() == 3
    # New event appended within the overlap window → only it is delivered; the
    # overlap re-read of the older three is de-duped by the seen set.
    await _append(
        outbox, kind=EVENT_KIND_FEED_POST, offset=3,
        character_id="char-1", post_id="post-9",
    )
    assert await disp.drain_once() == 1
    assert len(feed.events) == 4


@pytest.mark.asyncio
async def test_drain_advances_cursor_past_saturated_overlap_window() -> None:
    # Regression (sol #5): when the overlap window holds >= batch_limit rows the
    # cursor must still advance to the forward frontier — an out-of-order re-read
    # of the older rows can never wedge the loop on a fixed batch of old ids.
    outbox = InMemoryRealtimeOutbox()
    # batch_limit=3, overlap 30s. First drain the initial three (offsets 0-2).
    for i in range(3):
        await _append(
            outbox, kind=EVENT_KIND_FEED_POST, offset=i,
            character_id="char-1", post_id="post-9",
        )
    reh = _build(posts={"post-9": _post_row()})
    feed = _CapturingBus()
    disp = RealtimeEventDispatcher(
        outbox, reh, None, feed, batch_limit=3, overlap=timedelta(seconds=30),
    )
    assert await disp.drain_once() == 3  # cursor now at id=3

    # A new row lands within the overlap window: the window now holds 3 old ids
    # (== batch_limit) PLUS the new frontier row. The frontier must not starve.
    await _append(
        outbox, kind=EVENT_KIND_FEED_POST, offset=4,
        character_id="char-1", post_id="post-9",
    )
    assert await disp.drain_once() == 1  # the new row is delivered
    assert len(feed.events) == 4
    # The cursor advanced past the frontier: a further drain publishes nothing.
    assert await disp.drain_once() == 0


@pytest.mark.asyncio
async def test_drain_skips_rehydrate_miss_without_crashing() -> None:
    outbox = InMemoryRealtimeOutbox()
    await _append(
        outbox, kind=EVENT_KIND_FEED_POST, offset=0,
        character_id="char-1", post_id="gone",
    )
    await _append(
        outbox, kind=EVENT_KIND_FEED_POST, offset=1,
        character_id="char-1", post_id="post-9",
    )
    reh = _build(posts={"post-9": _post_row()})  # first row is a miss
    feed = _CapturingBus()
    disp = RealtimeEventDispatcher(outbox, reh, None, feed)

    published = await disp.drain_once()
    assert published == 1  # the miss is skipped, the live row delivered
    assert len(feed.events) == 1


@pytest.mark.asyncio
async def test_start_primes_cursor_at_tail_no_history_replay() -> None:
    outbox = InMemoryRealtimeOutbox()
    for i in range(3):
        await _append(
            outbox, kind=EVENT_KIND_FEED_POST, offset=i,
            character_id="char-1", post_id="post-9",
        )
    reh = _build(posts={"post-9": _post_row()})
    feed = _CapturingBus()
    disp = RealtimeEventDispatcher(
        outbox, reh, None, feed, prune_interval=0,
    )
    # start() primes cursor+seen at the tail; stop() before the manual drain
    # removes the background poll loop so the assertion is race-free.
    await disp.start()
    await disp.stop()
    assert feed.events == []  # the three historical rows were NOT replayed

    # Only an event appended AFTER start is delivered.
    await _append(
        outbox, kind=EVENT_KIND_FEED_POST, offset=3,
        character_id="char-1", post_id="post-9",
    )
    assert await disp.drain_once() == 1
    assert len(feed.events) == 1


@pytest.mark.asyncio
async def test_seen_set_evicts_past_overlap_window() -> None:
    outbox = InMemoryRealtimeOutbox()
    overlap = timedelta(seconds=10)
    # Two clusters far apart in time so the first cluster ages out of the seen
    # window after the second is processed.
    await _append(
        outbox, kind=EVENT_KIND_FEED_POST, offset=0,
        character_id="char-1", post_id="post-9",
    )
    reh = _build(posts={"post-9": _post_row()})
    feed = _CapturingBus()
    disp = RealtimeEventDispatcher(outbox, reh, None, feed, overlap=overlap)
    await disp.drain_once()
    assert len(disp._seen) == 1
    # A far-future event advances 'newest'; the old id ages past 2×overlap.
    await _append(
        outbox, kind=EVENT_KIND_FEED_POST, offset=100,
        character_id="char-1", post_id="post-9",
    )
    await disp.drain_once()
    # Only the recent id survives in the bounded seen set.
    assert len(disp._seen) == 1


@pytest.mark.asyncio
async def test_seen_set_honours_hard_cap_under_in_window_burst() -> None:
    # Regression (sol #20): a burst dense enough that nothing ages out of the
    # overlap window must still not grow the seen set past its hard cap.
    outbox = InMemoryRealtimeOutbox()
    # A wide overlap so time-based eviction never fires within the burst.
    disp = RealtimeEventDispatcher(
        outbox, _build(posts={"post-9": _post_row()}), None, _CapturingBus(),
        overlap=timedelta(days=365), batch_limit=1000, seen_hard_cap=10,
    )
    # 30 events all inside the window → time eviction keeps every id; only the
    # hard cap can bound the set.
    for i in range(30):
        await _append(
            outbox, kind=EVENT_KIND_FEED_POST, offset=i,
            character_id="char-1", post_id="post-9",
        )
    await disp.drain_once()
    assert len(disp._seen) == 10  # capped, not 30
    # The survivors are the newest ids (oldest-by-created_at were evicted).
    assert min(disp._seen) > 20
