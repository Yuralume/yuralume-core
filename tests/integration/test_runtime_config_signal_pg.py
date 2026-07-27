"""PostgreSQL integration proof for the two cross-process startup fixes.

Unit tests pin the decision logic against fakes; these pin the two things only a
real database can settle:

* the startup seed advisory lock genuinely **serialises** concurrent holders —
  the whole point of the Bug B fix is that seven processes stop overlapping, and
  a fake connection cannot demonstrate mutual exclusion;
* a real ``LISTEN``/``NOTIFY`` round-trip on the config channel wakes a
  refresher in a *different* connection (i.e. what a different process sees),
  and the fingerprint really tracks ``provider_connections`` mutations.

Requires Docker (testcontainers); skipped automatically when unavailable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from sqlalchemy import text

from kokoro_link.application.services.runtime_config_refresher import (
    RuntimeConfigRefresher,
)
from kokoro_link.bootstrap.startup_seed_lock import startup_seed_lock
from kokoro_link.contracts.runtime_config_events import RUNTIME_CONFIG_CHANNEL
from kokoro_link.infrastructure.persistence.realtime_listen import (
    build_listen_factory,
)
from kokoro_link.infrastructure.persistence.runtime_config_signal import (
    build_provider_fingerprint_reader,
    notify_runtime_config_changed,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 25, 9, 0, 0, tzinfo=timezone.utc)


async def _insert_provider_row(engine, connection_id: str, *, updated_at) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO provider_connections "
                "(id, provider, label, enabled, capabilities_json, config_json, "
                "encrypted_secret_json, secret_fingerprint, created_at, "
                "updated_at) VALUES (:id, 'openai', 'l', TRUE, '[\"llm\"]', "
                "'{}', '', '', :now, :updated)"
            ),
            {"id": connection_id, "now": BASE, "updated": updated_at},
        )


async def test_seed_lock_serialises_concurrent_holders(engine) -> None:
    """Two "processes" entering the seed batch at once must not overlap."""
    overlapped = False
    inside = 0

    async def _seed(marker: str, order: list[str]) -> None:
        nonlocal overlapped, inside
        async with startup_seed_lock(engine, timeout_seconds=30.0) as acquired:
            assert acquired is True
            inside += 1
            if inside > 1:
                overlapped = True
            order.append(f"{marker}-enter")
            # Yield generously: without the lock this is where the other task
            # would interleave (and it does — that is the bug being fixed).
            await asyncio.sleep(0.2)
            order.append(f"{marker}-exit")
            inside -= 1

    order: list[str] = []
    await asyncio.gather(_seed("a", order), _seed("b", order))

    assert overlapped is False
    # Strict alternation: one full enter/exit pair before the next enter.
    assert order[0].endswith("-enter")
    assert order[1] == order[0].replace("-enter", "-exit")
    assert order[2].endswith("-enter")
    assert order[3] == order[2].replace("-enter", "-exit")


async def test_seed_lock_is_released_for_the_next_process(engine) -> None:
    """A finished process must not leave the lock held for the fleet."""
    for _ in range(3):
        async with startup_seed_lock(engine, timeout_seconds=5.0) as acquired:
            assert acquired is True


async def test_fingerprint_tracks_insert_update_and_soft_delete(engine) -> None:
    read = build_provider_fingerprint_reader(engine)

    empty = await read()
    assert empty is not None

    await _insert_provider_row(engine, "p1", updated_at=BASE)
    after_insert = await read()
    assert after_insert != empty

    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE provider_connections SET enabled = FALSE, "
                "updated_at = :updated WHERE id = 'p1'"
            ),
            {"updated": BASE.replace(minute=5)},
        )
    after_disable = await read()
    assert after_disable != after_insert

    # Soft delete is the repository's delete path — it must move the
    # fingerprint too, or a removed credential would never propagate.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "UPDATE provider_connections SET deleted_at = :stamp, "
                "updated_at = :stamp WHERE id = 'p1'"
            ),
            {"stamp": BASE.replace(minute=9)},
        )
    assert await read() != after_disable


async def test_notify_wakes_a_refresher_on_another_connection(
    engine, postgres_container,
) -> None:
    """The Bug A end-to-end shape: process A writes + notifies, process B syncs."""
    from tests.conftest import _async_url

    synced = asyncio.Event()
    calls = 0

    async def _refresh() -> None:
        nonlocal calls
        calls += 1
        synced.set()

    # Process B: LISTEN on its own connection, poll cadence long enough that
    # only the notification can explain a sync.
    refresher = RuntimeConfigRefresher(
        refresh=_refresh,
        fingerprint=build_provider_fingerprint_reader(engine),
        listen_factory=build_listen_factory(
            _async_url(postgres_container), channel=RUNTIME_CONFIG_CHANNEL,
        ),
        poll_interval=300.0,
    )
    await refresher.start()
    try:
        # Let the LISTEN connection bind before the notification is sent —
        # PostgreSQL does not replay notifications to late subscribers.
        await asyncio.sleep(0.5)
        # start() always syncs once (the boot-window fix); that is the baseline
        # every assertion below counts from.
        assert calls == 1
        synced.clear()

        # Process A: the admin write path.
        await _insert_provider_row(engine, "p-notify", updated_at=BASE)
        assert await notify_runtime_config_changed(engine) is True

        await asyncio.wait_for(synced.wait(), timeout=10.0)
        assert calls == 2
    finally:
        await refresher.stop()


async def test_fallback_poll_converges_when_every_notification_is_lost(
    engine,
) -> None:
    """No LISTEN at all (dropped hint): the fingerprint gate still converges."""
    calls = 0

    async def _refresh() -> None:
        nonlocal calls
        calls += 1

    refresher = RuntimeConfigRefresher(
        refresh=_refresh,
        fingerprint=build_provider_fingerprint_reader(engine),
        poll_interval=0.05,
    )
    await refresher.start()
    try:
        await asyncio.sleep(0.3)
        # One boot sync, then nothing: the poll must NOT be writing runtime
        # status rows while the table is unchanged.
        assert calls == 1

        await _insert_provider_row(engine, "p-poll", updated_at=BASE)
        for _ in range(100):
            await asyncio.sleep(0.05)
            if calls > 1:
                break
        assert calls == 2

        # And it settles again rather than re-syncing every tick.
        await asyncio.sleep(0.3)
        assert calls == 2
    finally:
        await refresher.stop()
