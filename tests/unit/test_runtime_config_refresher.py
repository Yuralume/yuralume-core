"""Cross-process provider hot-reload: refresher + signal + wiring.

The bug this covers: ``sync_provider_connections`` rebuilds *process-local*
registries, and its only callers were app boot and the three admin write routes.
In a 7-process Hosted deploy an admin disabling a leaked API key therefore
reached exactly one api replica; the other api replica kept serving 50% of
requests with the old key, and coordinator/worker/connector (which serve no
admin route) never updated at all until restarted.

Everything here is driven with injected fakes — no database, no asyncpg — so the
decision logic is pinned independently of the transport.
"""

from __future__ import annotations

import asyncio

import pytest

from kokoro_link.application.services.runtime_config_refresher import (
    RuntimeConfigRefresher,
)
from kokoro_link.bootstrap.runtime_config_wiring import (
    build_runtime_config_refresher,
)
from kokoro_link.contracts.runtime_config_events import (
    RUNTIME_CONFIG_CHANNEL,
    RUNTIME_CONFIG_TOPIC_PROVIDERS,
)

pytestmark = pytest.mark.asyncio


class _Fingerprint:
    """A mutable fingerprint source, optionally failing."""

    def __init__(self, value: str | None = "v1") -> None:
        self.value = value
        self.reads = 0
        self.raises = False

    async def __call__(self) -> str | None:
        self.reads += 1
        if self.raises:
            raise RuntimeError("db down")
        return self.value


class _Refresh:
    def __init__(self) -> None:
        self.calls = 0
        self.raises = False

    async def __call__(self) -> None:
        self.calls += 1
        if self.raises:
            raise RuntimeError("sync exploded")


class _ListenSession:
    """Hands out one wake per queued nudge, then blocks forever."""

    def __init__(self) -> None:
        self.queue: asyncio.Queue[None] = asyncio.Queue()
        self.closed = False

    async def wait(self) -> None:
        await self.queue.get()

    async def close(self) -> None:
        self.closed = True


def _refresher(fingerprint, refresh, **kwargs) -> RuntimeConfigRefresher:
    return RuntimeConfigRefresher(
        refresh=refresh, fingerprint=fingerprint, **kwargs,
    )


# --- fingerprint gate -------------------------------------------------------


async def test_unchanged_fingerprint_does_not_sync() -> None:
    """The gate that stops 7 processes × N providers of status writes per tick."""
    fingerprint = _Fingerprint("v1")
    refresh = _Refresh()
    refresher = _refresher(fingerprint, refresh)
    await refresher.start()  # boot sync, then baselines at "v1"
    try:
        assert await refresher.refresh_once(force=False) is False
        # Still just the one unconditional boot sync.
        assert refresh.calls == 1
    finally:
        await refresher.stop()


async def test_changed_fingerprint_syncs() -> None:
    fingerprint = _Fingerprint("v1")
    refresh = _Refresh()
    refresher = _refresher(fingerprint, refresh)
    await refresher.start()
    try:
        fingerprint.value = "v2"
        assert await refresher.refresh_once(force=False) is True
        assert refresh.calls == 2  # boot sync + this one
        # Post-sync re-read is what is remembered, so the sync's own
        # record_runtime_status writes don't trigger a second sync.
        assert refresher.known_fingerprint == "v2"
        assert await refresher.refresh_once(force=False) is False
        assert refresh.calls == 2
    finally:
        await refresher.stop()


async def test_forced_refresh_syncs_even_when_fingerprint_is_unchanged() -> None:
    """NOTIFY is authoritative: the notifier saw a write we may not resolve."""
    fingerprint = _Fingerprint("v1")
    refresh = _Refresh()
    refresher = _refresher(fingerprint, refresh)
    await refresher.start()
    try:
        assert await refresher.refresh_once(force=True) is True
        assert refresh.calls == 2  # boot sync + the forced one
    finally:
        await refresher.stop()


async def test_failed_fingerprint_read_does_not_sync() -> None:
    """An unhealthy DB must not be answered with the more expensive operation."""
    fingerprint = _Fingerprint("v1")
    refresh = _Refresh()
    refresher = _refresher(fingerprint, refresh)
    await refresher.start()
    try:
        fingerprint.raises = True
        assert await refresher.refresh_once(force=False) is False
        assert refresh.calls == 1  # unchanged from the boot sync
    finally:
        await refresher.stop()


async def test_failed_sync_is_fail_soft_and_retried_next_tick() -> None:
    fingerprint = _Fingerprint("v1")
    refresh = _Refresh()
    refresher = _refresher(fingerprint, refresh)
    await refresher.start()
    try:
        fingerprint.value = "v2"
        refresh.raises = True
        assert await refresher.refresh_once(force=False) is False
        # Fingerprint NOT advanced → the next tick tries again.
        assert refresher.known_fingerprint == "v1"
        refresh.raises = False
        assert await refresher.refresh_once(force=False) is True
        assert refresh.calls == 3  # boot sync + failed attempt + retry
    finally:
        await refresher.stop()


# --- LISTEN path ------------------------------------------------------------


async def test_notification_triggers_a_sync() -> None:
    fingerprint = _Fingerprint("v1")
    refresh = _Refresh()
    session = _ListenSession()

    async def factory():
        return session

    refresher = _refresher(
        fingerprint,
        refresh,
        listen_factory=factory,
        # Long enough that only the NOTIFY can explain a sync.
        poll_interval=300.0,
    )
    await refresher.start()
    try:
        assert refresh.calls == 1  # the unconditional boot sync
        session.queue.put_nowait(None)
        for _ in range(200):
            await asyncio.sleep(0.005)
            if refresh.calls > 1:
                break
        # Fingerprint never moved: only the forced NOTIFY path can have synced.
        assert refresh.calls == 2
    finally:
        await refresher.stop()
    assert session.closed is True


async def test_stop_closes_the_listen_session_and_clears_tasks() -> None:
    fingerprint = _Fingerprint("v1")
    session = _ListenSession()

    async def factory():
        return session

    refresher = _refresher(
        fingerprint, _Refresh(), listen_factory=factory, poll_interval=300.0,
    )
    await refresher.start()
    await asyncio.sleep(0.01)
    await refresher.stop()
    assert session.closed is True
    # Idempotent: a second stop (double shutdown) must not raise.
    await refresher.stop()


async def test_start_forces_one_sync_then_settles() -> None:
    """The boot window: a change may have landed before this process listened.

    The caller's boot sync runs as the *first* startup seed, while ``start()``
    happens after all of them. A replica that changed a provider inside that
    window emits a NOTIFY nobody is listening for yet, and its write is already
    baked into the fingerprint ``start()`` would read — so priming from that read
    would gate every later poll to "unchanged" and strand this process on the old
    credentials until the next unrelated write or a restart. One unconditional
    sync at start closes it; the post-sync baseline keeps it to exactly one.
    """
    fingerprint = _Fingerprint("v2")
    refresh = _Refresh()
    refresher = _refresher(fingerprint, refresh, poll_interval=300.0)
    await refresher.start()
    try:
        assert refresh.calls == 1
        assert refresher.known_fingerprint == "v2"
        await asyncio.sleep(0.02)
        # Settles: the boot sync is not the start of a re-sync loop.
        assert refresh.calls == 1
        assert await refresher.refresh_once(force=False) is False
        assert refresh.calls == 1
    finally:
        await refresher.stop()


async def test_start_subscribes_to_listen_before_the_boot_sync() -> None:
    """Ordering matters: a change during the boot sync must still wake us."""
    order: list[str] = []
    session = _ListenSession()

    async def factory():
        order.append("listen")
        return session

    async def refresh() -> None:
        order.append("refresh")

    refresher = RuntimeConfigRefresher(
        refresh=refresh,
        fingerprint=_Fingerprint("v1"),
        listen_factory=factory,
        poll_interval=300.0,
    )
    await refresher.start()
    try:
        assert order == ["listen", "refresh"]
    finally:
        await refresher.stop()


async def test_failed_boot_sync_leaves_no_baseline_so_the_poll_retries() -> None:
    """Fail-soft at boot: a broken first sync must not be remembered as done."""
    fingerprint = _Fingerprint("v1")
    refresh = _Refresh()
    refresh.raises = True
    refresher = _refresher(fingerprint, refresh, poll_interval=300.0)
    await refresher.start()
    try:
        assert refresh.calls == 1
        assert refresher.known_fingerprint is None
        refresh.raises = False
        assert await refresher.refresh_once(force=False) is True
        assert refresher.known_fingerprint == "v1"
    finally:
        await refresher.stop()


# --- wiring -----------------------------------------------------------------


class _FakeDialect:
    def __init__(self, name: str) -> None:
        self.name = name


class _FakeEngine:
    def __init__(self, dialect_name: str) -> None:
        self.dialect = _FakeDialect(dialect_name)


async def _noop() -> None:
    return None


async def test_wiring_builds_a_refresher_on_postgres() -> None:
    refresher = build_runtime_config_refresher(
        engine=_FakeEngine("postgresql"),
        database_url="postgresql+asyncpg://u:p@h/db",
        refresh=_noop,
    )
    assert isinstance(refresher, RuntimeConfigRefresher)


async def test_wiring_routes_only_the_providers_topic() -> None:
    """The channel is shared with ``site_settings``; a provider re-sync writes a
    runtime-status row per enabled provider, so it must not be forced by an
    unrelated Admin save."""
    from kokoro_link.contracts.runtime_config_events import (
        RUNTIME_CONFIG_TOPIC_SITE_SETTINGS,
    )

    refresher = build_runtime_config_refresher(
        engine=_FakeEngine("postgresql"),
        database_url="postgresql+asyncpg://u:p@h/db",
        refresh=_noop,
    )
    assert refresher is not None
    assert refresher._wants(RUNTIME_CONFIG_TOPIC_PROVIDERS) is True
    assert refresher._wants(RUNTIME_CONFIG_TOPIC_SITE_SETTINGS) is False
    # Fail-open on an unknown / payload-less transport: a missed wake is a
    # correctness bug, a spurious one is a fingerprint-gated no-op.
    assert refresher._wants(None) is True


async def test_wiring_skips_sqlite_and_no_database() -> None:
    """Self-host red line: one process syncs itself inline, so nothing to wire."""
    assert build_runtime_config_refresher(
        engine=_FakeEngine("sqlite"),
        database_url="sqlite+aiosqlite:///./x.db",
        refresh=_noop,
    ) is None
    assert build_runtime_config_refresher(
        engine=None, database_url="", refresh=_noop,
    ) is None


async def test_config_channel_is_distinct_from_the_realtime_channel() -> None:
    from kokoro_link.contracts.realtime_events import REALTIME_EVENT_CHANNEL

    assert RUNTIME_CONFIG_CHANNEL != REALTIME_EVENT_CHANNEL
    assert RUNTIME_CONFIG_TOPIC_PROVIDERS == "providers"
