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
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
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

_LOGGER = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL_SECONDS = 15.0

router = APIRouter(tags=["events"])


def _encode_proactive_event(event: ProactiveEvent) -> str:
    payload = {
        "type": "proactive_message",
        "character_id": event.character_id,
        "conversation_id": event.conversation_id,
        "message": event.message,
        "unread_count": event.unread_count,
        "created_at": event.created_at.isoformat(),
    }
    return f"event: proactive_message\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _encode_feed_post_event(event: FeedPostEvent) -> str:
    payload = {
        "type": "feed_post",
        "character_id": event.character_id,
        "post_id": event.post_id,
        "kind": event.kind,
        "content_text": event.content_text,
        "image_url": event.image_url,
        "created_at": event.created_at.isoformat(),
    }
    return f"event: feed_post\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"


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
        f"event: feed_comment_reply\n"
        f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
    )


@router.get("/events/stream")
async def events_stream(
    request: Request,
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
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

    async def event_generator() -> AsyncIterator[str]:
        async with proactive_bus.subscription() as proactive_queue:
            async with _maybe_subscribe(feed_bus) as feed_queue:
                # Kick off with an immediate comment so the browser flips
                # EventSource.readyState to OPEN and any open() handler
                # finishes setup before real events arrive.
                yield ": connected\n\n"
                while True:
                    if await request.is_disconnected():
                        return
                    waiters: list[asyncio.Task] = [
                        asyncio.create_task(proactive_queue.get()),
                    ]
                    if feed_queue is not None:
                        waiters.append(asyncio.create_task(feed_queue.get()))
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
                    for task in done:
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
                        try:
                            if isinstance(event, FeedPostEvent):
                                yield _encode_feed_post_event(event)
                            elif isinstance(event, FeedCommentReplyEvent):
                                yield _encode_feed_reply_event(event)
                            elif isinstance(event, ProactiveEvent):
                                yield _encode_proactive_event(event)
                        except Exception:
                            _LOGGER.exception(
                                "failed to encode SSE event",
                            )

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
