"""API-side realtime outbox dispatcher (Phase 4 §7.1).

Each ``api`` replica runs one :class:`RealtimeEventDispatcher`. It tails the
durable PostgreSQL outbox and re-publishes every new event onto this process's
local :class:`ProactiveEventBus` / :class:`FeedEventBus`, so the SSE connections
this replica holds see scheduler-side (``background`` process) events without
any change to the SSE consumer code.

Flow per replica:

* On start the cursor is primed to the outbox's current ``latest_id`` — a fresh
  replica does NOT replay history to its live subscribers (that is the job of
  SSE ``Last-Event-ID`` replay, which is per-connection and client-driven).
* A dedicated ``LISTEN`` connection turns a best-effort ``NOTIFY`` into an
  immediate drain; a fallback poll (default 2s) drains regardless, so a dropped
  notification only costs latency, never delivery. The LISTEN connection
  self-heals: on any error it logs and rebuilds after a bounded backoff.
* Each drain reads ``read_after(cursor, overlap)`` — which re-surfaces the
  out-of-order-commit overlap tail (see the port docstring) — de-dupes by a
  bounded seen-id set (evicted by ``created_at`` once past the overlap window,
  plus a hard-count cap so a dense in-window burst can't grow it without bound),
  rehydrates every genuinely-new row to a full bus event, and publishes it.
* A row whose backing domain record was deleted rehydrates to ``None`` → skip +
  log; a single miss must never wedge the loop.
* A slow ``prune`` sweep (default 7d TTL, aligned with the background-jobs
  retention convention) keeps the table bounded. It lives here rather than in a
  separate maintenance job because the dispatcher already holds the outbox port
  on every api replica and the delete is idempotent, so N replicas pruning the
  same old rows is harmless — no new scheduler coupling for a bounded table.

``RealtimeEventRehydrator`` (the ids → full-event re-read) is shared verbatim
with the SSE ``Last-Event-ID`` replay path in ``api/routes/events.py``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable, Protocol

from kokoro_link.application.services.feed_event_bus import (
    FeedCommentReplyEvent,
    FeedPostEvent,
)
from kokoro_link.application.services.proactive_event_bus import ProactiveEvent
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.contracts.realtime_events import (
    DEFAULT_OVERLAP,
    EVENT_KIND_FEED_COMMENT_REPLY,
    EVENT_KIND_FEED_POST,
    EVENT_KIND_PROACTIVE,
    RealtimeOutboxPort,
    StoredRealtimeEvent,
)
from kokoro_link.domain.entities.conversation import MessageRole

_LOGGER = logging.getLogger(__name__)

RehydratedEvent = ProactiveEvent | FeedPostEvent | FeedCommentReplyEvent

# Fallback poll cadence + drain page size. The poll is only a safety net behind
# LISTEN; a large page keeps a burst from taking many round-trips.
_DEFAULT_POLL_INTERVAL = 2.0
_DEFAULT_BATCH_LIMIT = 256
_DEFAULT_PRUNE_INTERVAL = 3600.0
_DEFAULT_PRUNE_TTL = timedelta(days=7)
# Hard ceiling on the seen-id set. Time-based eviction (past 2×overlap) bounds it
# in the steady state, but a dense burst inside one window ages out nothing — so
# this cap guarantees the set stays bounded regardless of throughput, evicting
# the oldest ids (by created_at) once exceeded. Sized well above any realistic
# overlap-window population so it only ever bites a pathological burst.
_DEFAULT_SEEN_HARD_CAP = 50_000
# LISTEN reconnect backoff so a persistently-down database can't hot-loop.
_LISTEN_RECONNECT_BACKOFF = 2.0


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class _ConversationReader(Protocol):
    async def get(self, conversation_id: str) -> object | None: ...


class _FeedPostReader(Protocol):
    async def get(self, post_id: str) -> object | None: ...


class _FeedCommentReader(Protocol):
    async def get(self, comment_id: str) -> object | None: ...


class RealtimeEventRehydrator:
    """Re-reads the persisted domain rows named by an outbox payload and rebuilds
    the full bus event.

    The outbox row carries ids/counts ONLY (red line); the message / post /
    reply text lives in its own durable table, so we re-read it here. A missing
    row (deleted between append and read) yields ``None`` — the caller skips it.
    Shared by the dispatcher loop and SSE ``Last-Event-ID`` replay.
    """

    def __init__(
        self,
        conversations: _ConversationReader,
        feed_posts: _FeedPostReader,
        feed_comments: _FeedCommentReader,
    ) -> None:
        self._conversations = conversations
        self._feed_posts = feed_posts
        self._feed_comments = feed_comments

    async def rehydrate(
        self, stored: StoredRealtimeEvent,
    ) -> RehydratedEvent | None:
        try:
            if stored.event_kind == EVENT_KIND_PROACTIVE:
                return await self._rehydrate_proactive(stored)
            if stored.event_kind == EVENT_KIND_FEED_POST:
                return await self._rehydrate_feed_post(stored)
            if stored.event_kind == EVENT_KIND_FEED_COMMENT_REPLY:
                return await self._rehydrate_feed_comment_reply(stored)
        except Exception:  # a bad read must never wedge the drain loop
            _LOGGER.exception(
                "realtime rehydrate crashed (id=%s kind=%s)",
                stored.id, stored.event_kind,
            )
            return None
        _LOGGER.warning(
            "realtime rehydrate skipped unknown kind=%s (id=%s)",
            stored.event_kind, stored.id,
        )
        return None

    async def _rehydrate_proactive(
        self, stored: StoredRealtimeEvent,
    ) -> ProactiveEvent | None:
        payload = stored.payload
        conversation_id = str(payload.get("conversation_id", ""))
        if not conversation_id:
            return None
        conversation = await self._conversations.get(conversation_id)
        if conversation is None:
            _LOGGER.info(
                "realtime rehydrate: conversation %s gone (event id=%s)",
                conversation_id, stored.id,
            )
            return None
        # Pin to the exact assistant message this event was appended for — the
        # proactive publisher stamps that message with the same clock the outbox
        # row carries (``stored.created_at``). Reading "the latest assistant
        # message" instead would surface a NEWER reply if the player completed a
        # round between append and this rehydrate (sol #13). A missing match
        # (message edited / pruned in the race) skips — best-effort SSE.
        message = _assistant_text_at(conversation, stored.created_at)
        if message is None:
            return None
        return ProactiveEvent(
            character_id=str(payload.get("character_id", "")),
            conversation_id=conversation_id,
            message=message,
            created_at=stored.created_at,
            unread_count=int(payload.get("unread_count", 0) or 0),
            event_id=stored.id,
        )

    async def _rehydrate_feed_post(
        self, stored: StoredRealtimeEvent,
    ) -> FeedPostEvent | None:
        payload = stored.payload
        post_id = str(payload.get("post_id", ""))
        if not post_id:
            return None
        post = await self._feed_posts.get(post_id)
        if post is None:
            _LOGGER.info(
                "realtime rehydrate: feed post %s gone (event id=%s)",
                post_id, stored.id,
            )
            return None
        return FeedPostEvent(
            character_id=post.character_id,
            post_id=post.id,
            kind=post.kind.value,
            content_text=post.content_text,
            image_url=post.image_url,
            created_at=post.created_at,
            event_id=stored.id,
        )

    async def _rehydrate_feed_comment_reply(
        self, stored: StoredRealtimeEvent,
    ) -> FeedCommentReplyEvent | None:
        payload = stored.payload
        comment_id = str(payload.get("comment_id", ""))
        if not comment_id:
            return None
        comment = await self._feed_comments.get(comment_id)
        if comment is None:
            _LOGGER.info(
                "realtime rehydrate: feed comment %s gone (event id=%s)",
                comment_id, stored.id,
            )
            return None
        return FeedCommentReplyEvent(
            character_id=str(payload.get("character_id", "")),
            post_id=comment.post_id,
            comment_id=comment.id,
            content_text=comment.content_text,
            unread_count=int(payload.get("unread_count", 0) or 0),
            created_at=comment.created_at,
            event_id=stored.id,
        )


def _assistant_text_at(
    conversation: object, created_at: datetime,
) -> str | None:
    """Return the assistant message stamped at exactly ``created_at``.

    The proactive publisher stamps the assistant message with the same clock the
    outbox row carries, so this pins delivery to the message the event was
    appended for — never a newer reply the player produced in the race window
    between append and rehydrate (sol #13). Both sides are normalised to UTC
    before comparison so a naive/aware or offset mismatch can't spuriously miss.
    Best-effort: no match (message edited / pruned) simply skips.
    """
    target = ensure_utc(created_at)
    messages = getattr(conversation, "messages", None) or []
    for message in reversed(list(messages)):
        if getattr(message, "role", None) != MessageRole.ASSISTANT:
            continue
        stamp = getattr(message, "created_at", None)
        if stamp is not None and ensure_utc(stamp) == target:
            return message.content
    return None


# A LISTEN source: something that blocks until the next NOTIFY (or raises on a
# broken connection). Injected so the unit suite can run poll-only (``None``).
ListenWaiter = Callable[[], Awaitable[None]]
ListenFactory = Callable[[], Awaitable["ListenSession"]]


class ListenSession(Protocol):
    """One live LISTEN connection: wait for a nudge, then close."""

    async def wait(self) -> str | None:
        """Block until the next NOTIFY. Raise if the connection broke.

        Returns the notification payload when the transport carries one, so a
        subscriber that shares a channel with other topics can route on it (see
        :class:`~kokoro_link.application.services.runtime_config_refresher.RuntimeConfigRefresher`).
        ``None`` means "payload unknown" — a caller that routes on topics must
        treat that as *wake anyway*, never as "not mine".
        """
        ...

    async def close(self) -> None: ...


class RealtimeEventDispatcher:
    """Tails the outbox and republishes new events onto the local buses."""

    def __init__(
        self,
        outbox: RealtimeOutboxPort,
        rehydrator: RealtimeEventRehydrator,
        proactive_bus: object | None,
        feed_bus: object | None,
        *,
        poll_interval: float = _DEFAULT_POLL_INTERVAL,
        overlap: timedelta = DEFAULT_OVERLAP,
        batch_limit: int = _DEFAULT_BATCH_LIMIT,
        listen_factory: ListenFactory | None = None,
        prune_interval: float = _DEFAULT_PRUNE_INTERVAL,
        prune_ttl: timedelta = _DEFAULT_PRUNE_TTL,
        seen_hard_cap: int = _DEFAULT_SEEN_HARD_CAP,
        clock: Callable[[], datetime] = _now_utc,
    ) -> None:
        self._outbox = outbox
        self._rehydrator = rehydrator
        self._proactive_bus = proactive_bus
        self._feed_bus = feed_bus
        self._poll_interval = poll_interval
        self._overlap = overlap
        self._batch_limit = batch_limit
        self._listen_factory = listen_factory
        self._prune_interval = prune_interval
        self._prune_ttl = prune_ttl
        self._seen_hard_cap = max(1, seen_hard_cap)
        self._clock = clock

        self._cursor = 0
        # id -> created_at, bounded by eviction past the overlap window.
        self._seen: dict[int, datetime] = {}
        self._wake = asyncio.Event()
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []

    async def start(self) -> None:
        """Prime the cursor at the current tail and spin the loops.

        Priming the cursor alone is not enough: ``read_after`` re-surfaces the
        overlap tail (ids ``<= cursor`` within the window), so a fresh replica
        would replay that recent history to its live subscribers. We therefore
        also seed the seen-set with every id currently in the overlap window —
        marking them already-delivered — so only events appended *after* start
        reach the buses. A low-id row still uncommitted at start is not returned
        here, so its later commit is still delivered (anti-loss intact).
        """
        if self._running:
            return
        self._cursor = await self._outbox.latest_id()
        if self._cursor > 0:
            tail = await self._outbox.read_after(
                self._cursor, overlap=self._overlap, limit=self._batch_limit,
            )
            for stored in tail:
                self._seen[stored.id] = stored.created_at
        self._running = True
        self._tasks = [asyncio.create_task(self._poll_loop())]
        if self._listen_factory is not None:
            self._tasks.append(asyncio.create_task(self._listen_loop()))
        if self._prune_interval > 0:
            self._tasks.append(asyncio.create_task(self._prune_loop()))

    async def stop(self) -> None:
        """Cancel the loops and await their bounded teardown."""
        if not self._running:
            return
        self._running = False
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []

    # --- draining ---------------------------------------------------------

    async def drain_once(self) -> int:
        """Drain every event now visible past the cursor. Returns the count
        published. Exposed for the unit suite (poll-free determinism)."""
        published = 0
        while True:
            batch = await self._outbox.read_after(
                self._cursor, overlap=self._overlap, limit=self._batch_limit,
            )
            if not batch:
                break
            max_id = self._cursor
            for stored in batch:
                max_id = max(max_id, stored.id)
                if stored.id in self._seen:
                    continue
                self._seen[stored.id] = stored.created_at
                event = await self._rehydrator.rehydrate(stored)
                if event is not None:
                    await self._publish(event)
                    published += 1
            self._evict_seen()
            if max_id <= self._cursor:
                # No forward progress (only the overlap tail was re-read) → done.
                break
            self._cursor = max_id
            if len(batch) < self._batch_limit:
                break
        return published

    def _evict_seen(self) -> None:
        if not self._seen:
            return
        newest = max(self._seen.values())
        # Keep twice the overlap so a late low-id row still finds its id in the
        # seen set on the re-read; drop anything older.
        cutoff = newest - (self._overlap * 2)
        stale = [eid for eid, ts in self._seen.items() if ts < cutoff]
        for eid in stale:
            del self._seen[eid]
        # Hard cap: a burst dense enough that nothing ages out of the window must
        # still not grow the set without bound. Drop the oldest ids (by
        # created_at) down to the cap — re-delivery of an evicted-then-re-read id
        # is harmless (idempotent bus publish) versus unbounded memory.
        overflow = len(self._seen) - self._seen_hard_cap
        if overflow > 0:
            oldest = sorted(self._seen.items(), key=lambda kv: kv[1])[:overflow]
            for eid, _ts in oldest:
                del self._seen[eid]

    async def _publish(self, event: RehydratedEvent) -> None:
        try:
            if isinstance(event, ProactiveEvent):
                if self._proactive_bus is not None:
                    await self._proactive_bus.publish(event)
            elif self._feed_bus is not None:
                await self._feed_bus.publish(event)
        except Exception:
            _LOGGER.exception(
                "realtime dispatcher failed to publish event id=%s",
                event.event_id,
            )

    # --- loops ------------------------------------------------------------

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self.drain_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("realtime dispatcher drain failed")
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(
                    self._wake.wait(), timeout=self._poll_interval,
                )

    async def _listen_loop(self) -> None:
        assert self._listen_factory is not None
        while self._running:
            session: ListenSession | None = None
            try:
                session = await self._listen_factory()
                while self._running:
                    await session.wait()
                    self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.warning(
                    "realtime dispatcher LISTEN dropped; rebuilding",
                    exc_info=True,
                )
                await asyncio.sleep(_LISTEN_RECONNECT_BACKOFF)
            finally:
                if session is not None:
                    with contextlib.suppress(Exception):
                        await session.close()

    async def _prune_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._prune_interval)
            except asyncio.CancelledError:
                raise
            if not self._running:
                return
            try:
                removed = await self._outbox.prune(
                    self._clock() - self._prune_ttl,
                )
                if removed:
                    _LOGGER.info("realtime outbox pruned %d rows", removed)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception("realtime outbox prune failed")
