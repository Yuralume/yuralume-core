"""Phase 4 realtime-outbox wiring.

Decides — from the process-role component matrix and the realtime backend — how
the two in-process event buses are wired for a distributed deployment, replacing
the interim §7.0 HTTP relay. Kept out of ``container.py`` (already ~4k lines) as
a single pure function so the role→component mapping is unit-testable without a
database or a full container build.

Three shapes, one per side of the api/background split:

* ``memory`` backend (self-host default) OR no database → the raw in-process
  ``ProactiveEventBus`` / ``FeedEventBus`` pass straight through, byte-identical
  to the historical single-process behaviour (the self-host red line). No
  outbox, no dispatcher.
* ``postgres`` backend on the **writer** side (``enable_realtime_outbox_writer``
  — the ``background`` role that runs the schedulers, or the ``worker`` role that
  executes ticks, both serving no SSE) → the buses are wrapped with the outbox
  decorators so every proactive/feed publish also lands in the durable outbox.
  No dispatcher (this process holds no SSE connections).
* ``postgres`` backend on the **reader** side (serves API/SSE but runs no
  schedulers — the ``api`` role) → a :class:`RealtimeEventDispatcher` tails the
  outbox and re-publishes onto the raw local buses the SSE connections read, and
  the outbox + rehydrator are exposed for the SSE ``Last-Event-ID`` replay path.
  The buses handed back to the container are ALSO the outbox decorators, in
  reader mode (append-only, no local mirror) — see below.

The ``all`` role (schedulers AND SSE in one process, sharing one in-process bus)
needs neither side under ``postgres`` — the local bus already delivers every
event to the SSE connections in the same process — so it falls through to the
pass-through shape.

Why the reader side ALSO writes the outbox: an ``api`` replica is not a pure
reader. It publishes on request paths — ``POST /characters/{id}/feed`` (manual
post → ``FeedComposerService._publish``) and ``POST
/characters/{id}/proactive/evaluate`` (→ ``ProactiveDispatcher._deliver_web``).
With raw buses those events reached only the SSE subscribers held by the replica
that served the request: with N api replicas behind a round-robin, a client
parked on another replica never saw them, and because no outbox row existed a
``Last-Event-ID`` reconnect could not recover them either. Wrapping the reader's
buses puts api-originated events on exactly the same single delivery path as
worker-originated ones (publish → outbox → every replica's dispatcher → local
SSE), which also gives them the durable id the SSE ``id:`` line and replay need.
The decorators run in ``local_publish=False`` mode there so the publishing
replica's own subscribers are served once, by its dispatcher, not twice.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

from kokoro_link.application.services.feed_event_bus import FeedEventBus
from kokoro_link.application.services.proactive_event_bus import ProactiveEventBus
from kokoro_link.application.services.realtime_event_dispatcher import (
    RealtimeEventDispatcher,
    RealtimeEventRehydrator,
)
from kokoro_link.application.services.realtime_outbox_publisher import (
    OutboxFeedEventBus,
    OutboxProactiveEventBus,
    RealtimeOutboxPublisher,
)
from kokoro_link.bootstrap.process_roles import ComponentMatrix
from kokoro_link.contracts.realtime_events import RealtimeOutboxPort

_LOGGER = logging.getLogger(__name__)

REALTIME_BACKEND_POSTGRES = "postgres"


@dataclass(slots=True)
class RealtimeWiring:
    """Result of :func:`build_realtime_wiring`.

    ``proactive_bus`` / ``feed_bus`` are the buses the rest of the container must
    use downstream — the wrapped decorators on the writer side, or the raw buses
    everywhere else. ``outbox`` / ``rehydrator`` are set only on the api reader
    side (consumed by the SSE replay route); ``dispatcher`` only there too (the
    app lifespan starts / stops it).
    """

    proactive_bus: ProactiveEventBus
    feed_bus: FeedEventBus
    outbox: RealtimeOutboxPort | None = None
    rehydrator: RealtimeEventRehydrator | None = None
    dispatcher: RealtimeEventDispatcher | None = None


def build_realtime_wiring(
    *,
    matrix: ComponentMatrix,
    realtime_backend: str,
    database_url: str,
    db_session_factory: object | None,
    proactive_bus: ProactiveEventBus,
    feed_bus: FeedEventBus,
    conversations: object,
    feed_posts: object,
    feed_comments: object,
    poll_interval: float,
    outbox_factory: Callable[[], RealtimeOutboxPort] | None = None,
) -> RealtimeWiring:
    """Wire the realtime outbox for this process role. See the module docstring.

    ``db_session_factory`` is intentionally typed loosely (it is passed straight
    into :class:`SARealtimeOutbox`, which only stores it) so the mapping can be
    unit-tested with a sentinel — no engine, no connection. ``outbox_factory``
    is the matching seam for the outbox itself: production leaves it ``None``
    (an :class:`SARealtimeOutbox` is built), the unit suite injects one shared
    in-memory outbox across two wirings to prove cross-replica delivery.
    """

    if (
        realtime_backend != REALTIME_BACKEND_POSTGRES
        or db_session_factory is None
    ):
        # Self-host red line: raw in-process buses, no outbox, no dispatcher.
        _warn_if_publishes_into_the_void(matrix, realtime_backend)
        return RealtimeWiring(proactive_bus=proactive_bus, feed_bus=feed_bus)

    # Local import keeps the SA/asyncpg dependency off the in-memory path.
    from kokoro_link.infrastructure.persistence.realtime_listen import (
        build_listen_factory,
    )
    from kokoro_link.infrastructure.persistence.sa_realtime_events import (
        SARealtimeOutbox,
    )

    outbox: RealtimeOutboxPort = (
        outbox_factory()
        if outbox_factory is not None
        else SARealtimeOutbox(db_session_factory)  # type: ignore[arg-type]
    )
    publisher = RealtimeOutboxPublisher(
        outbox, session_factory=db_session_factory,  # type: ignore[arg-type]
    )

    # Writer side (background / worker): also land every publish in the durable
    # outbox so the api reader can tail it onto its SSE connections. The local
    # mirror stays on — these roles hold no SSE connections and run no
    # dispatcher, so nothing can re-deliver the event to them.
    if matrix.enable_realtime_outbox_writer:
        return RealtimeWiring(
            proactive_bus=OutboxProactiveEventBus(proactive_bus, publisher),
            feed_bus=OutboxFeedEventBus(feed_bus, publisher),
            outbox=outbox,
        )

    # Reader side (api): tail the outbox and re-publish onto the local buses the
    # SSE connections read. Only a role that serves SSE but runs no in-process
    # schedulers needs this — an ``all`` role's local bus already delivers.
    if matrix.serve_api_routes and not matrix.start_schedulers:
        rehydrator = RealtimeEventRehydrator(
            conversations, feed_posts, feed_comments,  # type: ignore[arg-type]
        )
        # The dispatcher publishes onto the RAW buses — never the decorators, or
        # a re-published event would be appended to the outbox again and loop.
        dispatcher = RealtimeEventDispatcher(
            outbox,
            rehydrator,
            proactive_bus,
            feed_bus,
            poll_interval=poll_interval,
            listen_factory=build_listen_factory(database_url),
        )
        # Request-path publishes on this replica go to the outbox ONLY: the
        # dispatcher above is the single delivery path for local subscribers,
        # so they are served exactly once and every other replica gets the event
        # too (see the module docstring).
        return RealtimeWiring(
            proactive_bus=OutboxProactiveEventBus(
                proactive_bus, publisher, local_publish=False,
            ),
            feed_bus=OutboxFeedEventBus(
                feed_bus, publisher, local_publish=False,
            ),
            outbox=outbox,
            rehydrator=rehydrator,
            dispatcher=dispatcher,
        )

    # all-role under postgres: single process, one shared in-process bus already
    # delivers to its own SSE connections — pass through unchanged.
    _warn_if_publishes_into_the_void(matrix, realtime_backend)
    return RealtimeWiring(proactive_bus=proactive_bus, feed_bus=feed_bus)


def _warn_if_publishes_into_the_void(
    matrix: ComponentMatrix, realtime_backend: str,
) -> None:
    """Log when a pass-through role could publish with nobody able to hear it.

    A pass-through wiring is only correct for the single-process ``all`` shape,
    where the same process both publishes and holds the SSE connections. Any
    OTHER role that actually publishes a proactive/feed bus event (an API,
    scheduler, or distributed worker) and lands here has no outbox to write and
    no local SSE subscriber to serve — the event is silently dropped.

    ``start_connectors`` is deliberately absent from the capability test:
    Telegram/Discord/WhatsApp connector loops transport chat messages through
    ``MessagingDispatcher`` but do not publish either realtime bus. Treating
    transport capability as realtime publication produced a false loss warning
    on every healthy dedicated connector boot.
    """

    can_publish = (
        matrix.serve_api_routes
        or matrix.start_schedulers
        or matrix.run_background_worker
    )
    is_single_process_shape = matrix.serve_api_routes and matrix.start_schedulers
    if not can_publish or is_single_process_shape:
        return
    _LOGGER.warning(
        "realtime: this role can publish proactive/feed events but wires "
        "neither the outbox writer nor a dispatcher (backend=%s) — any such "
        "publish reaches no SSE subscriber on any replica and leaves no "
        "replayable row. Give the role enable_realtime_outbox_writer if it "
        "ever publishes.",
        realtime_backend,
    )
