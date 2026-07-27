"""Writer-side realtime outbox publisher (Phase 4 §7.1).

The ``background`` process publishes proactive / feed events onto its
process-local :class:`ProactiveEventBus` / :class:`FeedEventBus`. Under the
``postgres`` realtime backend those buses are invisible to the SSE connections
served by the ``api`` process (§7.0), so this module also lands each event in
the durable PostgreSQL outbox that the api-side dispatcher (see
``realtime_event_dispatcher``) replays.

Two pieces:

* :class:`RealtimeOutboxPublisher` — turns a bus event into the pinned MINIMAL
  outbox payload (ids/counts only — never message/post text; the red line in
  ``contracts/realtime_events.py``) and appends it, then fires the best-effort
  ``NOTIFY`` wake hint **after** the append transaction commits.
* :class:`OutboxProactiveEventBus` / :class:`OutboxFeedEventBus` — duck-type
  identical bus decorators (same shape as the Phase 1 ``RealtimeRelay*`` buses)
  so wiring can swap them in without touching a single publish site. ``publish``
  keeps the local publish first (unchanged for anything subscribing in-process)
  then hooks the outbox append.

Two decorator modes — ``local_publish`` (see :class:`OutboxProactiveEventBus`):

* **writer mode** (``local_publish=True``, the default — ``background`` /
  ``worker``): local publish first, then the outbox append. Those roles hold no
  SSE connections and run no dispatcher, so the local publish reaches only
  in-process listeners and can never collide with an outbox re-delivery.
* **reader mode** (``local_publish=False`` — the ``api`` role): the api process
  ALSO publishes (manual feed post, proactive evaluate-now) while holding SSE
  connections AND running a :class:`RealtimeEventDispatcher` that tails the same
  outbox. Publishing locally *and* appending would deliver the event twice to
  this replica's own subscribers (once inline, once when its dispatcher reads
  the row back). So reader mode appends only: every subscriber on every replica
  — including the publishing one — is fed by a dispatcher, one path, once. The
  event also arrives carrying its durable outbox id, so its SSE frame gets an
  ``id:`` line and a ``Last-Event-ID`` reconnect can replay it.

Reader-mode fallback — appending only is safe **while the outbox is reachable**.
When the append fails there is no second path: nothing was written, so no
dispatcher on any replica will ever deliver the event, and reader mode would
have dropped it fleet-wide where the pre-outbox code still served this process's
own subscribers. ``RealtimeOutboxPublisher.publish`` therefore reports whether
the event became durable and reader mode publishes locally when it did not — a
degraded single-replica delivery instead of none. No double delivery is possible
either way: the fallback only runs when there is no row to come back round, and
a *notify* failure over a committed row still counts as durable.

Session discipline — why every site here is a **short-session append**, not a
same-transaction append:

  The three publish sites (``ProactiveDispatcher._deliver_web``,
  ``FeedComposerService._publish``, ``FeedCommentReplyService._broadcast``) run
  *after* their domain rows are already persisted through the repository layer,
  and that layer opens and commits its OWN short session per ``save()`` — there
  is no shared unit-of-work object in scope to ride. The domain write has
  therefore already committed by the time we publish, so the §7.1 "rollback must
  not leave a phantom event" hazard cannot occur here (a rolled-back domain
  write raises out of ``save()`` before we reach ``publish``). The residual gap
  — process death between the domain commit and the outbox append loses the SSE
  event — is acceptable: SSE is best-effort (the buses already drop on a full
  subscriber queue), and the durable chat / feed rows the user reloads are
  never affected.

The in-memory backend keeps its historical behaviour: no publisher is wired, so
the raw buses publish exactly as before (self-host red line).
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.application.services.feed_event_bus import (
    FeedBusEvent,
    FeedCommentReplyEvent,
    FeedEventBus,
    FeedPostEvent,
)
from kokoro_link.application.services.proactive_event_bus import (
    ProactiveEvent,
    ProactiveEventBus,
)
from kokoro_link.contracts.realtime_events import (
    EVENT_KIND_FEED_COMMENT_REPLY,
    EVENT_KIND_FEED_POST,
    EVENT_KIND_PROACTIVE,
    RealtimeEventSpec,
    RealtimeOutboxPort,
)

_LOGGER = logging.getLogger(__name__)

# Everything the publisher can serialise. Kept in lockstep with the dispatcher
# rehydrator on the api side.
OutboxableEvent = ProactiveEvent | FeedBusEvent


def event_to_outbox_spec(event: OutboxableEvent) -> RealtimeEventSpec:
    """Map a bus event to its MINIMAL outbox spec (ids/counts only).

    ``created_at`` is taken from the event itself — the same domain clock the
    persisted row was stamped with — so the reader's overlap-window fencing and
    the stored timestamp share one clock (never a server-side ``now()``). Raises
    ``TypeError`` for an unrelayable type so a caller mistake fails loud.
    """

    if isinstance(event, ProactiveEvent):
        return RealtimeEventSpec(
            event_kind=EVENT_KIND_PROACTIVE,
            payload={
                "character_id": event.character_id,
                "conversation_id": event.conversation_id,
                "unread_count": event.unread_count,
            },
            created_at=event.created_at,
        )
    if isinstance(event, FeedPostEvent):
        return RealtimeEventSpec(
            event_kind=EVENT_KIND_FEED_POST,
            payload={
                "character_id": event.character_id,
                "post_id": event.post_id,
            },
            created_at=event.created_at,
        )
    if isinstance(event, FeedCommentReplyEvent):
        return RealtimeEventSpec(
            event_kind=EVENT_KIND_FEED_COMMENT_REPLY,
            payload={
                "character_id": event.character_id,
                "post_id": event.post_id,
                "comment_id": event.comment_id,
                "unread_count": event.unread_count,
            },
            created_at=event.created_at,
        )
    raise TypeError(
        f"cannot append event of type {type(event).__name__} to the outbox",
    )


class RealtimeOutboxPublisher:
    """Appends bus events to the durable outbox with a post-commit wake hint.

    ``session_factory`` opens the short session the append rides (the domain
    rows already committed independently — see the module docstring). Pass
    ``None`` to drive an in-memory outbox in the unit suite: the in-memory
    adapter's append ignores the session and needs no commit.
    """

    def __init__(
        self,
        outbox: RealtimeOutboxPort,
        session_factory: sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self._outbox = outbox
        self._session_factory = session_factory

    async def publish(self, event: OutboxableEvent) -> bool:
        """Append ``event`` to the outbox and fire its NOTIFY hint.

        Returns whether the event became **durable**, i.e. whether the outbox —
        and therefore every replica's dispatcher — will see it. Reader mode has
        no second delivery path, so that answer is what lets its decorator fall
        back to a local publish instead of dropping the event fleet-wide (see
        the module docstring).

        Never raises into the caller: a failed append costs one SSE nudge (a
        reconnecting browser re-reads durable rows anyway). The wake hint is
        fired only after a successful commit so a replica never wakes to an
        uncommitted row and spins on an empty read.
        """

        try:
            spec = event_to_outbox_spec(event)
        except TypeError:
            _LOGGER.exception("realtime outbox: unrelayable event type")
            return False

        try:
            if self._session_factory is None:
                stored = await self._outbox.append(_NULL_SESSION, spec)
            else:
                async with self._session_factory() as session:
                    stored = await self._outbox.append(session, spec)
                    await session.commit()
        except Exception:  # noqa: BLE001 — SSE nudge is best-effort
            _LOGGER.warning(
                "realtime outbox append failed (kind=%s)",
                spec.event_kind,
                exc_info=True,
            )
            return False

        # Domain + event both durable — now (and only now) nudge the replicas.
        # A lost nudge is NOT a lost event: the row is committed and the next
        # dispatcher poll picks it up, so this still reports success. Reporting
        # failure here would make reader mode publish locally on top of the
        # dispatcher's delivery and serve its own subscribers twice.
        try:
            await self._outbox.notify_hint(event_id=stored.id)
        except Exception:  # noqa: BLE001 — the row is already durable
            _LOGGER.warning(
                "realtime outbox notify hint failed (id=%s)",
                stored.id,
                exc_info=True,
            )
        return True


# Sentinel passed to the in-memory adapter's append (which ignores it).
_NULL_SESSION: Any = object()


async def _publish_through_outbox(
    inner: "ProactiveEventBus | FeedEventBus",
    publisher: RealtimeOutboxPublisher,
    event: OutboxableEvent,
    *,
    local_publish: bool,
) -> None:
    """Shared ``publish`` body for both decorator buses.

    The two decorators are duck-type identical, so the mode logic — and the
    reader-mode fallback in particular — lives here once rather than being
    copied and then drifting apart on the next fix.
    """

    if local_publish:
        await inner.publish(event)
    try:
        durable = await publisher.publish(event)
    except Exception:  # belt-and-braces: outbox must not break publish
        _LOGGER.exception("realtime outbox publish hook failed")
        durable = False
    if durable or local_publish:
        return
    # Reader mode with nothing in the outbox means NOBODY receives this event:
    # this role deliberately publishes nothing locally, and no dispatcher — here
    # or on any other replica — has a row to read. Degrading to the pre-outbox
    # behaviour serves at least our own SSE connections, and cannot double-
    # deliver: a row that was never written can never come back round.
    _LOGGER.warning(
        "realtime outbox unavailable; falling back to local-only realtime "
        "delivery for this replica's subscribers",
    )
    await inner.publish(event)


class OutboxProactiveEventBus:
    """``ProactiveEventBus`` decorator that also lands events in the outbox.

    Subscription surface delegates to the wrapped bus so any in-process SSE keeps
    working; only ``publish`` gains the durable-append side effect.

    ``local_publish`` picks the mode (see the module docstring): ``True`` (writer
    roles) mirrors onto the wrapped bus before appending; ``False`` (the api
    reader role, which runs a dispatcher over the same outbox) appends only, so
    the dispatcher is the single delivery path and no subscriber is served twice.
    """

    def __init__(
        self,
        inner: ProactiveEventBus,
        publisher: RealtimeOutboxPublisher,
        *,
        local_publish: bool = True,
    ) -> None:
        self._inner = inner
        self._publisher = publisher
        self._local_publish = local_publish

    @property
    def mirrors_locally(self) -> bool:
        """Whether ``publish`` also delivers to this process's subscribers."""

        return self._local_publish

    async def publish(self, event: ProactiveEvent) -> None:
        await _publish_through_outbox(
            self._inner, self._publisher, event,
            local_publish=self._local_publish,
        )

    def subscribe(self) -> "Any":
        return self._inner.subscribe()

    def unsubscribe(self, queue: "Any") -> None:
        self._inner.unsubscribe(queue)

    def subscription(self) -> "contextlib.AbstractAsyncContextManager[Any]":
        return self._inner.subscription()

    def subscriber_count(self) -> int:
        return self._inner.subscriber_count()


class OutboxFeedEventBus:
    """``FeedEventBus`` decorator — same shape as ``OutboxProactiveEventBus``."""

    def __init__(
        self,
        inner: FeedEventBus,
        publisher: RealtimeOutboxPublisher,
        *,
        local_publish: bool = True,
    ) -> None:
        self._inner = inner
        self._publisher = publisher
        self._local_publish = local_publish

    @property
    def mirrors_locally(self) -> bool:
        """Whether ``publish`` also delivers to this process's subscribers."""

        return self._local_publish

    async def publish(self, event: FeedBusEvent) -> None:
        await _publish_through_outbox(
            self._inner, self._publisher, event,
            local_publish=self._local_publish,
        )

    def subscribe(self) -> "Any":
        return self._inner.subscribe()

    def unsubscribe(self, queue: "Any") -> None:
        self._inner.unsubscribe(queue)

    def subscription(self) -> "contextlib.AbstractAsyncContextManager[Any]":
        return self._inner.subscription()

    def subscriber_count(self) -> int:
        return self._inner.subscriber_count()
