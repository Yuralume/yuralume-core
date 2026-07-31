"""Phase 4 realtime-outbox wiring (HOSTED_CORE_SCALING_ARCHITECTURE_PLAN §7.1).

Covers the role→component mapping in ``bootstrap/realtime_wiring`` (unit-tested
with a sentinel session factory — no engine, no connection) plus the self-host
red line proved through the real ``build_container``: the memory default builds
byte-identically to the historical single-process container (raw buses, no
outbox, no dispatcher).

Also covers the api-side publish path (§7.1 follow-up): an ``api`` replica also
publishes (manual feed post, proactive evaluate-now), so its buses are wrapped
with the outbox decorators in **reader mode** — publish writes the outbox ONLY
and every local SSE subscriber is fed by this replica's own dispatcher, so both
replicas see the event exactly once through one delivery path.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.feed_event_bus import (
    FeedEventBus,
    FeedPostEvent,
)
from kokoro_link.application.services.proactive_event_bus import ProactiveEventBus
from kokoro_link.application.services.realtime_event_dispatcher import (
    RealtimeEventDispatcher,
    RealtimeEventRehydrator,
)
from kokoro_link.application.services.realtime_outbox_publisher import (
    OutboxFeedEventBus,
    OutboxProactiveEventBus,
)
from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.process_roles import matrix_for_role
from kokoro_link.bootstrap.process_settings import ProcessSettings
from kokoro_link.bootstrap.realtime_wiring import build_realtime_wiring
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.infrastructure.repositories.in_memory_realtime_events import (
    InMemoryRealtimeOutbox,
)

_SENTINEL_FACTORY = object()  # SARealtimeOutbox only stores it — never called.


def _wire(role: str, *, backend: str, db=_SENTINEL_FACTORY, **overrides):
    kwargs = {
        "conversations": object(),
        "feed_posts": object(),
        "feed_comments": object(),
    }
    kwargs.update(overrides)
    return build_realtime_wiring(
        matrix=matrix_for_role(role),
        realtime_backend=backend,
        database_url="postgresql+asyncpg://u:p@localhost/db",
        db_session_factory=db,
        proactive_bus=ProactiveEventBus(),
        feed_bus=FeedEventBus(),
        poll_interval=2.0,
        **kwargs,
    )


# --------------------------------------------------------------------------- #
# Pure wiring function — one shape per side of the api/background split
# --------------------------------------------------------------------------- #


def test_api_postgres_builds_dispatcher_and_replay_surface() -> None:
    wiring = _wire("api", backend="postgres")
    # Reader side: the dispatcher + replay surface are wired, AND the buses the
    # rest of the container publishes through are the outbox decorators in
    # reader mode — an api-originated publish (manual feed post / proactive
    # evaluate-now) must reach the OTHER api replica, so it goes to the outbox
    # like every worker-side event.
    assert isinstance(wiring.proactive_bus, OutboxProactiveEventBus)
    assert isinstance(wiring.feed_bus, OutboxFeedEventBus)
    assert wiring.proactive_bus.mirrors_locally is False
    assert wiring.feed_bus.mirrors_locally is False
    assert isinstance(wiring.dispatcher, RealtimeEventDispatcher)
    assert isinstance(wiring.rehydrator, RealtimeEventRehydrator)
    assert wiring.outbox is not None


def test_writer_roles_keep_local_mirror_semantics() -> None:
    # Zero change to the worker/background side: they hold no SSE connections
    # and no dispatcher, so the local publish stays (historical behaviour).
    for role in ("background", "worker"):
        wiring = _wire(role, backend="postgres")
        assert wiring.proactive_bus.mirrors_locally is True
        assert wiring.feed_bus.mirrors_locally is True


def test_background_postgres_wraps_buses_without_dispatcher() -> None:
    wiring = _wire("background", backend="postgres")
    # Writer side: buses are the durable-outbox decorators; no dispatcher (this
    # process holds no SSE connections).
    assert isinstance(wiring.proactive_bus, OutboxProactiveEventBus)
    assert isinstance(wiring.feed_bus, OutboxFeedEventBus)
    assert wiring.dispatcher is None
    assert wiring.rehydrator is None
    assert wiring.outbox is not None


def test_all_postgres_passes_through_single_process_bus() -> None:
    # One process runs schedulers AND SSE on one shared in-process bus — neither
    # decorator nor dispatcher is needed.
    wiring = _wire("all", backend="postgres")
    assert type(wiring.proactive_bus) is ProactiveEventBus
    assert type(wiring.feed_bus) is FeedEventBus
    assert wiring.dispatcher is None
    assert wiring.outbox is None


def test_worker_postgres_wraps_buses_without_dispatcher() -> None:
    # §2.1 dedicated worker: it executes ticks (publishing proactive/feed events)
    # but serves no SSE → writer side (outbox decorators), no dispatcher.
    wiring = _wire("worker", backend="postgres")
    assert isinstance(wiring.proactive_bus, OutboxProactiveEventBus)
    assert isinstance(wiring.feed_bus, OutboxFeedEventBus)
    assert wiring.dispatcher is None
    assert wiring.rehydrator is None
    assert wiring.outbox is not None


def test_coordinator_postgres_does_not_decorate_buses() -> None:
    # A pure coordinator only enqueues — it never publishes proactive/feed events,
    # so its buses stay RAW (no writer decoration) and it holds no SSE (no reader
    # dispatcher). ``outbox`` is still built (harmless read port), never wrapped.
    wiring = _wire("coordinator", backend="postgres")
    assert type(wiring.proactive_bus) is ProactiveEventBus
    assert type(wiring.feed_bus) is FeedEventBus
    assert wiring.dispatcher is None
    assert wiring.rehydrator is None


def test_connector_postgres_has_no_realtime_component() -> None:
    # §2.1 dedicated connector: no realtime component at all — raw buses, no
    # writer decoration, no reader dispatcher.
    wiring = _wire("connector", backend="postgres")
    assert type(wiring.proactive_bus) is ProactiveEventBus
    assert type(wiring.feed_bus) is FeedEventBus
    assert wiring.dispatcher is None
    assert wiring.rehydrator is None


def test_connector_postgres_does_not_emit_false_realtime_loss_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Messaging connectors do not publish proactive/feed realtime-bus events;
    # treating ``start_connectors`` as a realtime publisher makes every healthy
    # dedicated connector boot look like player-event loss.
    _wire("connector", backend="postgres")

    assert "reaches no SSE subscriber" not in caplog.text


def test_memory_backend_is_pure_passthrough_every_role() -> None:
    for role in ("all", "api", "background", "coordinator", "worker", "connector"):
        wiring = _wire(role, backend="memory")
        assert type(wiring.proactive_bus) is ProactiveEventBus
        assert type(wiring.feed_bus) is FeedEventBus
        assert wiring.outbox is None
        assert wiring.rehydrator is None
        assert wiring.dispatcher is None


def test_postgres_backend_without_database_is_passthrough() -> None:
    # postgres backend but no DB session factory → raw buses (the wiring never
    # fabricates an outbox it can't back).
    wiring = _wire("api", backend="postgres", db=None)
    assert type(wiring.proactive_bus) is ProactiveEventBus
    assert wiring.dispatcher is None
    assert wiring.outbox is None


# --------------------------------------------------------------------------- #
# Two api replicas, one delivery path (the api-publish gap)
# --------------------------------------------------------------------------- #

BASE = datetime(2026, 7, 25, 9, 0, 0, tzinfo=timezone.utc)


class _FakeSession:
    """Short session the outbox append rides — the in-memory outbox ignores it."""

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def commit(self) -> None:
        return None


def _fake_session_factory() -> _FakeSession:
    return _FakeSession()


class _Kind:
    def __init__(self, value: str) -> None:
        self.value = value


class _StoredPost:
    """Minimal ``feed_posts.get`` row the rehydrator re-reads."""

    def __init__(self) -> None:
        self.id = "post-42"
        self.character_id = "char-1"
        self.kind = _Kind("daily")
        self.content_text = "手動發的一篇"
        self.image_url = None
        self.created_at = BASE


class _PostRepo:
    def __init__(self, post: _StoredPost) -> None:
        self._post = post

    async def get(self, post_id: str) -> _StoredPost | None:
        return self._post if post_id == self._post.id else None


def _api_replica(outbox: InMemoryRealtimeOutbox, posts: _PostRepo):
    return _wire(
        "api",
        backend="postgres",
        db=_fake_session_factory,
        feed_posts=posts,
        outbox_factory=lambda: outbox,
    )


@pytest.mark.asyncio
async def test_api_publish_reaches_both_replicas_exactly_once() -> None:
    """The gap this wiring closes: a manual feed post created on api-1.

    Before, api-1 published onto its raw local bus only — api-2's SSE clients
    never saw it and no outbox row existed to replay. Now the publish writes the
    outbox and BOTH replicas' dispatchers fan it out to their own subscribers,
    each exactly once (api-1 must not also deliver it locally at publish time).
    """
    outbox = InMemoryRealtimeOutbox()
    post = _StoredPost()
    posts = _PostRepo(post)
    api1 = _api_replica(outbox, posts)
    api2 = _api_replica(outbox, posts)

    # SSE clients subscribe through the container-facing bus (the decorator
    # delegates its subscription surface to the raw bus the dispatcher feeds).
    async with api1.feed_bus.subscription() as q1:
        async with api2.feed_bus.subscription() as q2:
            await api1.feed_bus.publish(FeedPostEvent(
                character_id=post.character_id,
                post_id=post.id,
                kind=post.kind.value,
                content_text=post.content_text,
                image_url=post.image_url,
                created_at=post.created_at,
            ))
            # No local short-circuit: the only delivery path is the outbox.
            assert q1.empty()
            assert q2.empty()

            assert await api1.dispatcher.drain_once() == 1
            assert await api2.dispatcher.drain_once() == 1

            got1 = q1.get_nowait()
            got2 = q2.get_nowait()
            # Exactly once per replica — no double-send on the publishing one.
            assert q1.empty()
            assert q2.empty()

    assert got1.post_id == post.id
    assert got2.post_id == post.id
    # Delivered through the outbox → carries the durable id the SSE frame emits
    # as ``id:`` and a reconnect resumes from.
    assert got1.event_id == got2.event_id
    assert got1.event_id is not None
    # Re-draining delivers nothing new (seen-id de-dup holds across replicas).
    assert await api1.dispatcher.drain_once() == 0
    assert await api2.dispatcher.drain_once() == 0


@pytest.mark.asyncio
async def test_api_published_event_is_replayable_from_the_outbox() -> None:
    # ``Last-Event-ID`` replay reads the same rows: an api-originated event is
    # now recoverable after a reconnect (it used to exist only in RAM).
    outbox = InMemoryRealtimeOutbox()
    post = _StoredPost()
    api1 = _api_replica(outbox, _PostRepo(post))

    await api1.feed_bus.publish(FeedPostEvent(
        character_id=post.character_id,
        post_id=post.id,
        kind=post.kind.value,
        content_text=post.content_text,
        image_url=post.image_url,
        created_at=post.created_at,
    ))

    rows = await api1.outbox.read_after(0, limit=10)
    assert [r.payload["post_id"] for r in rows] == [post.id]
    replayed = await api1.rehydrator.rehydrate(rows[0])
    assert replayed is not None
    assert replayed.post_id == post.id
    assert replayed.event_id == rows[0].id


@pytest.mark.asyncio
async def test_api_publish_failure_never_raises_into_the_caller() -> None:
    # Fail-soft parity with the writer side: a broken outbox must not turn
    # ``POST /characters/{id}/feed`` into a 5xx.
    class _BoomOutbox(InMemoryRealtimeOutbox):
        async def append(self, session: object, spec: object):  # type: ignore[override]
            raise RuntimeError("db down")

    api1 = _api_replica(_BoomOutbox(), _PostRepo(_StoredPost()))
    await api1.feed_bus.publish(FeedPostEvent(
        character_id="char-1",
        post_id="post-42",
        kind="daily",
        content_text="x",
        image_url=None,
        created_at=BASE,
    ))


# --------------------------------------------------------------------------- #
# Self-host red line through the real container — memory default unchanged
# --------------------------------------------------------------------------- #


def _memory_container(role: str):
    return build_container(
        AppSettings(database_url="", process=ProcessSettings(role=role)),
    )


def test_container_memory_default_has_raw_buses_and_no_outbox() -> None:
    for role in ("all", "api", "background"):
        container = _memory_container(role)
        assert type(container.proactive_event_bus) is ProactiveEventBus
        assert type(container.feed_event_bus) is FeedEventBus
        assert container.realtime_outbox is None
        assert container.realtime_rehydrator is None
        assert container.realtime_dispatcher is None
