"""In-process pub/sub for feed-wall events.

Mirror of :mod:`proactive_event_bus` — same single-writer / multi-reader
shape, same drop-on-full policy, separate type so SSE encoders can
fan-out the streams without runtime ``isinstance`` checks. Published
*after* the post is persisted so subscribers always see a row that's
already queryable via the REST API.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator

_LOGGER = logging.getLogger(__name__)

_SUBSCRIBER_QUEUE_MAX = 64


@dataclass(frozen=True, slots=True)
class FeedPostEvent:
    """Payload broadcast when a character publishes a feed post."""

    character_id: str
    post_id: str
    kind: str
    content_text: str
    image_url: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class FeedCommentReplyEvent:
    """Broadcast when a scheduler-tick character reply lands.

    The frontend uses this to bump the LumeGram launcher badge in
    real time (without polling). ``unread_count`` is the post-increment
    counter so the UI can render the exact value the next character
    fetch would return — saves an extra GET round-trip."""

    character_id: str
    post_id: str
    comment_id: str
    content_text: str
    unread_count: int
    created_at: datetime


# Union type for everything the bus may carry. Listed inline because
# ``isinstance`` switches in the SSE encoder are still the cleanest way
# to discriminate one channel into two SSE event names.
FeedBusEvent = FeedPostEvent | FeedCommentReplyEvent


class FeedEventBus:
    """Fan-out bus for feed events (posts + character replies)."""

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[FeedBusEvent]] = set()

    def subscribe(self) -> asyncio.Queue[FeedBusEvent]:
        queue: asyncio.Queue[FeedBusEvent] = asyncio.Queue(
            maxsize=_SUBSCRIBER_QUEUE_MAX,
        )
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[FeedBusEvent]) -> None:
        self._subscribers.discard(queue)

    @contextlib.asynccontextmanager
    async def subscription(
        self,
    ) -> AsyncIterator[asyncio.Queue[FeedBusEvent]]:
        queue = self.subscribe()
        try:
            yield queue
        finally:
            self.unsubscribe(queue)

    async def publish(self, event: FeedBusEvent) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                _LOGGER.warning(
                    "feed event bus dropped event for slow subscriber "
                    "(character_id=%s)",
                    event.character_id,
                )

    def subscriber_count(self) -> int:
        return len(self._subscribers)
