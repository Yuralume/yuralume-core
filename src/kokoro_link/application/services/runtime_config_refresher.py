"""Per-process refresher that keeps runtime registries in step with the DB.

Every Core process (api / coordinator / worker / connector) rebuilds its
in-memory provider registries once at boot. Before this component that was the
*only* rebuild outside the api replica that served an admin write, so a
credential change reached at most one of seven processes (see
:mod:`kokoro_link.contracts.runtime_config_events` for why that is a security
problem and not merely staleness).

The refresher closes the loop with the same two-track discipline the realtime
outbox dispatcher uses, for the same reason (a ``NOTIFY`` may be dropped, so it
can only ever be a latency optimisation):

* a dedicated ``LISTEN`` connection turns a change notification into an
  immediate re-sync, self-healing on any error after a bounded backoff;
* a slow fallback poll re-reads a **cheap fingerprint** of the config table and
  re-syncs only when it moved.

The fingerprint gate is not an optimisation, it is a requirement: a re-sync
writes a runtime-status row per enabled provider, so an unconditional periodic
sync would multiply into (processes × providers) writes every poll interval.
Gating on the fingerprint keeps the steady state at exactly one cheap read per
process per interval.

Both tracks feed a single driver loop (the poll loop), so a notification and a
poll can never run two syncs concurrently — the sync tears down and rebuilds a
shared registry, and an interleaving could leave a provider transiently
unregistered.

Transport-agnostic by construction: the refresh callback, the fingerprint
reader and the LISTEN factory are all injected, so the unit suite drives it with
no database at all, and a non-PostgreSQL (self-host SQLite) deployment simply
never builds one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Awaitable, Callable

from kokoro_link.application.services.realtime_event_dispatcher import (
    ListenFactory,
    ListenSession,
)

_LOGGER = logging.getLogger(__name__)

# Fallback poll cadence. Slow on purpose: LISTEN carries the latency-sensitive
# path, and this is only the safety net for a dropped notification. Every tick
# costs one cheap aggregate read (see the fingerprint gate note above).
DEFAULT_POLL_INTERVAL = 45.0

# LISTEN reconnect backoff so a persistently-down database cannot hot-loop.
_LISTEN_RECONNECT_BACKOFF = 2.0

RefreshCallback = Callable[[], Awaitable[None]]
"""Rebuilds this process's registries from the database (the DB is the SoT)."""

FingerprintReader = Callable[[], Awaitable[str | None]]
"""Cheap "has the config table moved?" probe. ``None`` = read failed."""


class RuntimeConfigRefresher:
    """Keeps one process's runtime registries converged with the config table."""

    def __init__(
        self,
        *,
        refresh: RefreshCallback,
        fingerprint: FingerprintReader,
        listen_factory: ListenFactory | None = None,
        poll_interval: float = DEFAULT_POLL_INTERVAL,
        label: str = "providers",
        topics: frozenset[str] | None = None,
    ) -> None:
        self._refresh_cb = refresh
        self._fingerprint = fingerprint
        self._listen_factory = listen_factory
        self._poll_interval = poll_interval
        self._label = label
        # Which NOTIFY payloads on the shared channel belong to this refresher.
        # ``None`` = wake on everything (the pre-topic behaviour). Routing
        # matters because the refreshes are NOT the same price: a provider
        # re-sync writes a runtime-status row per enabled provider, so letting a
        # site-settings change force one on every process would reintroduce
        # exactly the write amplification the fingerprint gate exists to stop.
        self._topics = topics

        self._known_fingerprint: str | None = None
        self._wake = asyncio.Event()
        self._running = False
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def known_fingerprint(self) -> str | None:
        """Last fingerprint this process is known to be converged with."""

        return self._known_fingerprint

    async def start(self) -> None:
        """Subscribe, converge once, then spin the loops.

        The order is the whole point, because ``start()`` does not happen at the
        same instant as the caller's boot sync — that sync is the *first* startup
        seed and this runs after all of them:

        1. **LISTEN first.** PostgreSQL does not replay notifications to a late
           subscriber, so anything that lands while we are syncing must already
           have somewhere to land.
        2. **One unconditional sync.** A replica that changed a provider during
           the boot window notified an audience that did not include us yet, and
           its write is already baked into the fingerprint we are about to read.
           Priming straight from that read would gate every later poll to
           "unchanged" and leave this process serving the *old* credentials until
           some unrelated write moved the table again — the exact security
           failure this component exists to prevent. One extra sync per process
           per boot is the cheap side of that trade.
        3. **Baseline afterwards.** ``refresh_once`` stores the post-sync
           fingerprint, so the boot sync's own runtime-status writes do not read
           back as "the table moved" and start a re-sync loop. A *failed* boot
           sync deliberately leaves no baseline, so the first poll retries.
        """
        if self._running:
            return
        self._running = True
        self._tasks = []
        if self._listen_factory is not None:
            self._tasks.append(asyncio.create_task(self._listen_loop()))
            # Hand the loop one turn so the subscription is actually being
            # established before the sync starts, rather than merely scheduled.
            await asyncio.sleep(0)
        await self.refresh_once(force=True)
        # Started last: the poll loop is the single driver of syncs (the LISTEN
        # loop only sets ``_wake``), so nothing can run concurrently with the
        # boot sync above. A notification that arrived during it left the event
        # set, and this loop's first pass picks it up.
        self._tasks.append(asyncio.create_task(self._poll_loop()))

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

    async def refresh_once(self, *, force: bool) -> bool:
        """Re-sync when the config moved (or unconditionally when ``force``).

        Returns whether the refresh callback actually ran. Exposed so the unit
        suite can drive the decision logic without spinning the loops.

        ``force`` is the notification path: the notifier already knows the table
        changed, and its own write may share a fingerprint with our last read
        (same second, same row count), so re-deriving the answer from the
        fingerprint would be strictly weaker than the signal we were handed.
        """
        fingerprint = await self._read_fingerprint()
        if not force:
            if fingerprint is None:
                # Fingerprint read failed — do NOT fall through to a sync. The
                # database is unhealthy, and a sync is far more expensive than
                # the read that just failed. The next tick retries.
                return False
            if fingerprint == self._known_fingerprint:
                return False
        try:
            await self._refresh_cb()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Fail-soft: a failed refresh leaves ``_known_fingerprint`` behind
            # the table, so the next tick retries rather than silently
            # accepting stale registries.
            _LOGGER.exception("runtime config refresh failed (%s)", self._label)
            return False
        # Re-read AFTER the sync rather than storing the pre-sync value: the
        # sync itself writes runtime-status rows for the providers it (re)built,
        # which moves the fingerprint. Storing the post-sync value stops this
        # process from re-syncing in response to its own write.
        self._known_fingerprint = await self._read_fingerprint()
        return True

    def _wants(self, payload: str | None) -> bool:
        """Whether a notification payload is one this refresher should act on.

        Fail-open by design: an unset ``topics`` filter, or a transport that
        does not surface the payload, wakes us. A missed wake would be a
        correctness bug (stale config until the next poll); a spurious one only
        costs a fingerprint-gated no-op.
        """
        if self._topics is None or payload is None:
            return True
        return payload in self._topics

    async def _read_fingerprint(self) -> str | None:
        try:
            return await self._fingerprint()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.warning(
                "runtime config fingerprint read failed (%s)",
                self._label,
                exc_info=True,
            )
            return None

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(
                        self._wake.wait(), timeout=self._poll_interval,
                    )
                if not self._running:
                    return
                # Clear BEFORE the refresh, not after: a notification that lands
                # while the sync is in flight re-arms the event and the next
                # pass picks it up, instead of being swallowed by a late clear.
                forced = self._wake.is_set()
                self._wake.clear()
                await self.refresh_once(force=forced)
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.exception(
                    "runtime config refresh loop failed (%s)", self._label,
                )

    async def _listen_loop(self) -> None:
        assert self._listen_factory is not None
        while self._running:
            session: ListenSession | None = None
            try:
                session = await self._listen_factory()
                while self._running:
                    payload = await session.wait()
                    if not self._wants(payload):
                        continue
                    self._wake.set()
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.warning(
                    "runtime config LISTEN dropped; rebuilding (%s)",
                    self._label,
                    exc_info=True,
                )
                await asyncio.sleep(_LISTEN_RECONNECT_BACKOFF)
            finally:
                if session is not None:
                    with contextlib.suppress(Exception):
                        await session.close()
