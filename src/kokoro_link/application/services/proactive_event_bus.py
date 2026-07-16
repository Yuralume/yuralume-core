"""In-process pub/sub for proactive-message events.

Single-writer (the dispatcher) / multi-reader (SSE subscribers). The
bus is intentionally process-local — the app runs as one uvicorn
worker in the single-user deployment model we target, so there is
nothing to replicate across. If we ever grow to multiple workers this
is where Redis pub/sub would plug in.

Events are published *after* the assistant message is persisted, so
subscribers can trust the conversation already reflects the new turn
when they receive the event.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator

_LOGGER = logging.getLogger(__name__)

# Bounded per-subscriber queue. Proactive messages arrive at human
# speed (minutes apart); a large buffer would hide slow subscribers
# instead of dropping them. 64 is generous.
_SUBSCRIBER_QUEUE_MAX = 64


@dataclass(frozen=True, slots=True)
class ProactiveEvent:
    """Payload broadcast when a proactive message is delivered to the
    web channel.

    ``unread_count`` is the counter *after* the increment, so clients
    can update their badge state without a follow-up fetch.
    """

    character_id: str
    conversation_id: str
    message: str
    created_at: datetime
    unread_count: int


class ProactiveEventBus:
    """Fan-out bus. Each subscriber gets its own bounded queue.

    Slow subscribers get their messages dropped (log + continue) rather
    than blocking the dispatcher — a stuck SSE client must not stall
    LLM-driven proactive delivery for every other open tab.
    """

    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue[ProactiveEvent]] = set()

    def subscribe(self) -> asyncio.Queue[ProactiveEvent]:
        queue: asyncio.Queue[ProactiveEvent] = asyncio.Queue(
            maxsize=_SUBSCRIBER_QUEUE_MAX,
        )
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[ProactiveEvent]) -> None:
        self._subscribers.discard(queue)

    @contextlib.asynccontextmanager
    async def subscription(
        self,
    ) -> AsyncIterator[asyncio.Queue[ProactiveEvent]]:
        queue = self.subscribe()
        try:
            yield queue
        finally:
            self.unsubscribe(queue)

    async def publish(self, event: ProactiveEvent) -> None:
        # Snapshot so a subscriber unsubscribing mid-publish doesn't
        # raise RuntimeError: Set changed size during iteration.
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                _LOGGER.warning(
                    "proactive event bus dropped event for slow subscriber "
                    "(character_id=%s)", event.character_id,
                )

    def subscriber_count(self) -> int:
        return len(self._subscribers)
