"""Unit tests for SSE ``Last-Event-ID`` replay + memory-backend invariance.

Two guarantees:

* memory backend unchanged — with no durable outbox id on an event, the SSE
  frame carries no ``id:`` line, byte-identical to the historical stream;
* postgres backend — a reconnect with ``Last-Event-ID`` replays outbox rows
  (id-carrying frames) then joins the live bus, de-duping the boundary.

Times are self-referential data anchored to a fixed base, never the wall clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kokoro_link.api.routes import events as events_route
from kokoro_link.application.services.feed_event_bus import (
    FeedEventBus,
    FeedPostEvent,
)
from kokoro_link.application.services.proactive_event_bus import (
    ProactiveEvent,
    ProactiveEventBus,
)
from kokoro_link.application.services.realtime_event_dispatcher import (
    RealtimeEventRehydrator,
    RealtimeRehydrateTransientError,
)
from kokoro_link.contracts.realtime_events import (
    EVENT_KIND_FEED_POST,
    RealtimeEventSpec,
)
from kokoro_link.infrastructure.repositories.in_memory_realtime_events import (
    InMemoryRealtimeOutbox,
)

BASE = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


class _DictReader:
    def __init__(self, rows):
        self._rows = rows

    async def get(self, key):
        return self._rows.get(key)


def _post_row(post_id="post-9"):
    return SimpleNamespace(
        id=post_id, character_id="char-1", kind=SimpleNamespace(value="daily"),
        content_text="散步中", image_url=None, created_at=BASE,
    )


# --- memory-backend invariance: no id: line -------------------------------

def test_encoder_omits_id_when_event_has_none() -> None:
    event = ProactiveEvent(
        character_id="char-1", conversation_id="conv-1", message="hi",
        created_at=BASE, unread_count=1,
    )
    frame = events_route._encode_proactive_event(event)
    assert "id:" not in frame
    assert frame.startswith("event: proactive_message\n")


def test_encoder_includes_id_when_event_carries_outbox_id() -> None:
    event = ProactiveEvent(
        character_id="char-1", conversation_id="conv-1", message="hi",
        created_at=BASE, unread_count=1, event_id=7,
    )
    frame = events_route._encode_proactive_event(event)
    assert frame.startswith("id: 7\nevent: proactive_message\n")


def test_feed_post_encoder_id_line_only_with_outbox_id() -> None:
    no_id = FeedPostEvent(
        character_id="char-1", post_id="p", kind="daily",
        content_text="x", image_url=None, created_at=BASE,
    )
    with_id = FeedPostEvent(
        character_id="char-1", post_id="p", kind="daily",
        content_text="x", image_url=None, created_at=BASE, event_id=3,
    )
    assert "id:" not in events_route._encode_feed_post_event(no_id)
    assert events_route._encode_feed_post_event(with_id).startswith("id: 3\n")


def test_parse_last_event_id_lenient() -> None:
    assert events_route._parse_last_event_id("5") == 5
    assert events_route._parse_last_event_id(None) is None
    assert events_route._parse_last_event_id("") is None
    assert events_route._parse_last_event_id("abc") is None
    assert events_route._parse_last_event_id("0") is None
    assert events_route._parse_last_event_id("-3") is None


# --- replay_frames helper -------------------------------------------------

async def _seed(outbox, post_id, offset):
    return await outbox.append(
        None,
        RealtimeEventSpec(
            event_kind=EVENT_KIND_FEED_POST,
            payload={"character_id": "char-1", "post_id": post_id},
            created_at=BASE + timedelta(seconds=offset),
        ),
    )


async def _always_owned(_cid):
    return True


@pytest.mark.asyncio
async def test_replay_frames_emits_id_frames_and_records_seen() -> None:
    outbox = InMemoryRealtimeOutbox()
    await _seed(outbox, "post-9", 0)
    r2 = await _seed(outbox, "post-9", 1)
    reh = RealtimeEventRehydrator(
        _DictReader({}), _DictReader({"post-9": _post_row()}), _DictReader({}),
    )
    replayed: set[int] = set()
    frames = [
        f async for f in events_route.replay_frames(
            outbox, reh, resume_from=1, is_owned=_always_owned,
            replayed_ids=replayed,
        )
    ]
    # Responsibility boundary (sol #12): replay is deliberately at-least-once.
    # ``resume_from=1`` is the last id the client already CONFIRMED, yet the
    # overlap re-read re-emits it (id=1) alongside the genuinely-new frontier
    # (id=2). The server does NOT suppress the confirmed id — that is the
    # client's job (see ``utils/eventStreamDedup``); here we only assert the
    # server re-surfaces both with id: lines so the client can de-dup by id.
    assert all(f.startswith("id: ") for f in frames)
    assert 1 in replayed  # the already-confirmed id is re-delivered, by design
    assert r2.id in replayed  # the forward frontier is delivered too
    assert replayed == {1, r2.id}


@pytest.mark.asyncio
async def test_replay_frames_skips_missing_row() -> None:
    outbox = InMemoryRealtimeOutbox()
    await _seed(outbox, "gone", 0)
    reh = RealtimeEventRehydrator(
        _DictReader({}), _DictReader({}), _DictReader({}),  # row deleted
    )
    replayed: set[int] = set()
    frames = [
        f async for f in events_route.replay_frames(
            outbox, reh, resume_from=1, is_owned=_always_owned,
            replayed_ids=replayed,
        )
    ]
    assert frames == []
    assert replayed == {1}  # id recorded even though rehydrate missed


@pytest.mark.asyncio
async def test_replay_transient_failure_does_not_mark_event_replayed() -> None:
    outbox = InMemoryRealtimeOutbox()
    stored = await _seed(outbox, "post-9", 0)

    class _TransientRehydrator:
        async def rehydrate(self, _stored):
            raise RealtimeRehydrateTransientError("temporary read failure")

    replayed: set[int] = set()
    frames = [
        frame async for frame in events_route.replay_frames(
            outbox, _TransientRehydrator(), resume_from=stored.id,
            is_owned=_always_owned, replayed_ids=replayed,
        )
    ]

    assert frames == []
    assert replayed == set()


@pytest.mark.asyncio
async def test_replay_frames_respects_owner_filter() -> None:
    outbox = InMemoryRealtimeOutbox()
    await _seed(outbox, "post-9", 0)
    reh = RealtimeEventRehydrator(
        _DictReader({}), _DictReader({"post-9": _post_row()}), _DictReader({}),
    )

    async def _never(_cid):
        return False

    frames = [
        f async for f in events_route.replay_frames(
            outbox, reh, resume_from=1, is_owned=_never, replayed_ids=set(),
        )
    ]
    assert frames == []


# --- full route: replay then live, boundary de-duped ----------------------

class _FakeRequest:
    def __init__(self) -> None:
        self._disconnected = False

    def disconnect(self) -> None:
        self._disconnected = True

    async def is_disconnected(self) -> bool:
        return self._disconnected


def _container(outbox, rehydrator):
    return SimpleNamespace(
        proactive_event_bus=ProactiveEventBus(),
        feed_event_bus=FeedEventBus(),
        realtime_outbox=outbox,
        realtime_rehydrator=rehydrator,
    )


@pytest.mark.asyncio
async def test_route_replays_then_joins_live_without_duplicating_boundary() -> None:
    outbox = InMemoryRealtimeOutbox()
    stored = await _seed(outbox, "post-9", 0)  # id=1
    reh = RealtimeEventRehydrator(
        _DictReader({}), _DictReader({"post-9": _post_row()}), _DictReader({}),
    )
    container = _container(outbox, reh)
    request = _FakeRequest()

    response = await events_route.events_stream(
        request, container=container, current_user_id="u1",
        last_event_id=str(stored.id),
    )
    gen = response.body_iterator

    # 1) connected prelude, 2) replay frame for id=1 (subscription is now live).
    connected = await gen.__anext__()
    assert connected == ": connected\n\n"
    replay_frame = await gen.__anext__()
    assert replay_frame.startswith("id: 1\nevent: feed_post\n")

    # Publish two live events: one duplicates the replayed id=1 (must be
    # dropped at the boundary), one is new id=2.
    feed_bus = container.feed_event_bus
    await feed_bus.publish(FeedPostEvent(
        character_id="char-1", post_id="post-9", kind="daily",
        content_text="散步中", image_url=None, created_at=BASE, event_id=1,
    ))
    await feed_bus.publish(FeedPostEvent(
        character_id="char-1", post_id="post-9", kind="daily",
        content_text="又出門了", image_url=None, created_at=BASE, event_id=2,
    ))

    # The next yielded live frame is id=2 — id=1 was silently de-duped.
    live_frame = await gen.__anext__()
    assert live_frame.startswith("id: 2\nevent: feed_post\n")

    request.disconnect()
    await gen.aclose()
