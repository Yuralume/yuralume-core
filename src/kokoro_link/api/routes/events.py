"""Server-sent events stream for in-app push.

One long-lived ``GET /api/v1/events/stream`` connection per browser
tab multiplexes two in-process buses:

* ``ProactiveEventBus`` — proactive chat messages (sidebar badge, chat
  toast).
* ``FeedEventBus`` — feed-wall posts (動態 panel refresh).

Each event lands as its own discriminated SSE frame (``event:
proactive_message`` / ``event: feed_post``) so the client can dispatch
without parsing the JSON ``type`` field. A 15-second heartbeat keeps
intermediate proxies from killing idle connections.

This is deliberately *not* a per-character stream — sidebar / feed
panel both need updates for any character, so the single global
channel is simpler than multiplexing dozens of connections. Events
carry their own ``character_id`` so clients can filter locally.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from kokoro_link.api.dependencies import (
    get_container,
    get_current_user_id,
)
from kokoro_link.application.services.feed_event_bus import (
    FeedCommentReplyEvent,
    FeedPostEvent,
)
from kokoro_link.application.services.proactive_event_bus import ProactiveEvent
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.realtime_events import DEFAULT_OVERLAP

_LOGGER = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_SECONDS = 15.0
# Cap on how many outbox rows a single ``Last-Event-ID`` reconnect replays, so a
# browser that was offline for a long time can't force an unbounded replay. The
# 7-day TTL prune already bounds the table; this bounds one connection's burst.
_REPLAY_LIMIT = 512

router = APIRouter(tags=["events"])


def _id_prefix(event: object) -> str:
    """``id: <n>\\n`` when the event carries a durable outbox id (postgres
    backend), else empty — so the in-memory backend frame is byte-identical to
    the historical single-process output (no ``id:`` line, no replay anchor)."""
    event_id = getattr(event, "event_id", None)
    return "" if event_id is None else f"id: {event_id}\n"


def _encode_proactive_event(event: ProactiveEvent) -> str:
    payload = {
        "type": "proactive_message",
        "character_id": event.character_id,
        "conversation_id": event.conversation_id,
        "message": event.message,
        "unread_count": event.unread_count,
        "created_at": event.created_at.isoformat(),
    }
    return (
        f"{_id_prefix(event)}event: proactive_message\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def _encode_feed_post_event(event: FeedPostEvent) -> str:
    payload = {
        "type": "feed_post",
        "character_id": event.character_id,
        "post_id": event.post_id,
        "kind": event.kind,
        "content_text": event.content_text,
        "image_url": event.image_url,
        "video_url": event.video_url,
        "created_at": event.created_at.isoformat(),
    }
    return (
        f"{_id_prefix(event)}event: feed_post\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def _encode_feed_reply_event(event: FeedCommentReplyEvent) -> str:
    payload = {
        "type": "feed_comment_reply",
        "character_id": event.character_id,
        "post_id": event.post_id,
        "comment_id": event.comment_id,
        "content_text": event.content_text,
        "unread_count": event.unread_count,
        "created_at": event.created_at.isoformat(),
    }
    return (
        f"{_id_prefix(event)}event: feed_comment_reply\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


def _encode_event(event: object) -> str | None:
    if isinstance(event, FeedPostEvent):
        return _encode_feed_post_event(event)
    if isinstance(event, FeedCommentReplyEvent):
        return _encode_feed_reply_event(event)
    if isinstance(event, ProactiveEvent):
        return _encode_proactive_event(event)
    return None


def _parse_last_event_id(raw: str | None) -> int | None:
    """A browser resends the last ``id:`` it saw as ``Last-Event-ID``. Parse it
    leniently — any non-integer means "no anchor", i.e. start live only."""
    if not raw:
        return None
    try:
        value = int(raw.strip())
    except ValueError:
        return None
    return value if value > 0 else None


async def replay_frames(
    outbox: object,
    rehydrator: object,
    resume_from: int,
    is_owned: "Callable[[str | None], Awaitable[bool]]",
    replayed_ids: set[int],
) -> AsyncIterator[str]:
    """Re-emit outbox events after ``resume_from`` as encoded SSE frames.

    Shared rehydrate + encode path with the dispatcher. Populates
    ``replayed_ids`` so the caller's live loop can de-dupe the replay/live
    boundary. Reads the out-of-order overlap tail (``read_after``) and de-dupes
    it by id within the replay itself. Fail-soft: any read/rehydrate error is
    logged and never raised into the stream. A transient id is not added to
    ``replayed_ids``, so the later live-dispatch retry remains deliverable.
    """
    try:
        rows = await outbox.read_after(
            resume_from, overlap=DEFAULT_OVERLAP, limit=_REPLAY_LIMIT,
        )
    except Exception:
        _LOGGER.exception("SSE replay read_after failed")
        return
    for stored in rows:
        if stored.id in replayed_ids:
            continue
        try:
            event = await rehydrator.rehydrate(stored)
        except Exception:
            _LOGGER.exception("SSE replay rehydrate failed id=%s", stored.id)
            continue
        replayed_ids.add(stored.id)
        if event is None:
            continue
        if not await is_owned(getattr(event, "character_id", None)):
            continue
        frame = _encode_event(event)
        if frame is not None:
            yield frame


@router.get("/events/stream")
async def events_stream(
    request: Request,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    proactive_bus = container.proactive_event_bus
    if proactive_bus is None:
        # Happens in test harnesses that build a bare ServiceContainer
        # without the proactive stack. Fail loud rather than serve an
        # always-silent stream the operator would never notice.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="proactive event bus not configured",
        )
    feed_bus = container.feed_event_bus

    # Phase 4 replay wiring — present only under the ``postgres`` realtime
    # backend (the wiring task sets these container attributes). On the
    # in-memory backend both are ``None``: no ``id:`` frames, no replay, wire
    # output identical to the historical single-process stream (self-host red
    # line). ``getattr`` keeps this route forward-compatible before wiring lands.
    realtime_outbox = getattr(container, "realtime_outbox", None)
    realtime_rehydrator = getattr(container, "realtime_rehydrator", None)
    resume_from = _parse_last_event_id(last_event_id)

    # GD1-A. An SSE connection is in-flight work as far as uvicorn's graceful
    # shutdown is concerned, so a replica that does not close its streams burns
    # the whole ``stop_grace_period`` on idle sockets and is SIGKILLed anyway —
    # the api role's share of the GD0 grace only lands once these close. ``None``
    # on hand-built containers, where the loop below is byte-identical to before.
    drain_state = getattr(container, "drain_state", None)

    async def _is_owned(character_id: str | None) -> bool:
        """Server-side gate: only forward events whose character is
        owned by the connected user. Cached per request so we don't hit
        the service for every event."""
        if character_id is None:
            return True
        if character_id in _OWNED:
            return _OWNED[character_id]
        service = getattr(container, "character_service", None)
        if service is None:
            # Test stubs without character_service can't enforce — fall
            # back to forwarding so single-user harnesses don't break.
            _OWNED[character_id] = True
            return True
        try:
            character = await service.get_character_entity(
                character_id, user_id=current_user_id,
            )
        except TypeError:
            character = await service.get_character_entity(character_id)
            if (
                character is not None
                and getattr(character, "user_id", current_user_id)
                != current_user_id
            ):
                character = None
        owned = character is not None
        _OWNED[character_id] = owned
        return owned

    _OWNED: dict[str, bool] = {}

    can_replay = (
        realtime_outbox is not None
        and realtime_rehydrator is not None
        and resume_from is not None
    )

    async def event_generator() -> AsyncIterator[str]:
        async with proactive_bus.subscription() as proactive_queue:
            async with _maybe_subscribe(feed_bus) as feed_queue:
                # Kick off with an immediate comment so the browser flips
                # EventSource.readyState to OPEN and any open() handler
                # finishes setup before real events arrive.
                yield ": connected\n\n"
                # Last-Event-ID replay (postgres backend only). Subscribed
                # above already, so live events published during replay queue up
                # and are de-duped by replayed_ids at the live boundary below.
                replayed_ids: set[int] = set()
                if can_replay:
                    async for frame in replay_frames(
                        realtime_outbox, realtime_rehydrator,
                        resume_from, _is_owned, replayed_ids,
                    ):
                        yield frame
                while True:
                    if await request.is_disconnected():
                        return
                    # Drain requested before this connection even reached the
                    # loop (router already moved on): close now rather than hold
                    # a socket the deploy is waiting on.
                    if drain_state is not None and drain_state.draining:
                        return
                    waiters: list[asyncio.Task] = [
                        asyncio.create_task(proactive_queue.get()),
                    ]
                    if feed_queue is not None:
                        waiters.append(asyncio.create_task(feed_queue.get()))
                    # Woken by the drain endpoint, so the close happens in
                    # milliseconds instead of on the next 15s heartbeat. The
                    # deploy's 180s budget is for finishing *turns*, not for
                    # waiting out an idle stream's tick.
                    drain_waiter: asyncio.Task | None = None
                    if drain_state is not None:
                        drain_waiter = asyncio.create_task(
                            drain_state.wait_draining(),
                        )
                        waiters.append(drain_waiter)
                    try:
                        done, pending = await asyncio.wait(
                            waiters,
                            timeout=_HEARTBEAT_INTERVAL_SECONDS,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                    except asyncio.CancelledError:
                        for task in waiters:
                            task.cancel()
                        raise
                    for task in pending:
                        task.cancel()
                    if not done:
                        yield ": ping\n\n"
                        continue
                    draining_now = (
                        drain_waiter is not None and drain_waiter in done
                    )
                    for task in done:
                        if task is drain_waiter:
                            continue
                        try:
                            event = task.result()
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            _LOGGER.exception(
                                "event-stream waiter crashed",
                            )
                            continue
                        # Server-side owner filter — drop events for
                        # characters this user doesn't own so SSE
                        # subscribers can't see each other's traffic.
                        if not await _is_owned(
                            getattr(event, "character_id", None),
                        ):
                            continue
                        # Replay/live boundary de-dup: an event already sent by
                        # the replay prologue must not be re-delivered live.
                        event_id = getattr(event, "event_id", None)
                        if event_id is not None and event_id in replayed_ids:
                            continue
                        try:
                            frame = _encode_event(event)
                            if frame is not None:
                                yield frame
                        except Exception:
                            _LOGGER.exception(
                                "failed to encode SSE event",
                            )
                    if draining_now:
                        # Plain ``return``: the generator finishes, the
                        # subscriptions unwind through their context managers,
                        # and the client sees a clean end-of-stream. Raising
                        # here would surface as a broken connection and cost the
                        # browser its ``EventSource`` backoff before it retries.
                        # Anything that was ready this tick has already been
                        # flushed above, and under the postgres backend the
                        # reconnect replays from ``Last-Event-ID`` anyway.
                        return

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


import contextlib


@contextlib.asynccontextmanager
async def _maybe_subscribe(bus):
    """Optional ``async with`` for buses that may be ``None``.

    Keeps the event-generator body branch-free — when no feed bus is
    wired (e.g. test harness that skips the feed stack), the inner
    queue is simply ``None`` and the wait loop ignores it.
    """
    if bus is None:
        yield None
        return
    async with bus.subscription() as queue:
        yield queue
