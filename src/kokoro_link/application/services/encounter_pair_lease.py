"""Per-pair execution lease session for encounter background jobs (Phase 5 §6.1).

An ``encounter_tick`` job runs on a PAIR of characters. Before any LLM call the
handler must hold BOTH characters atomically so no second, crossing pair can
drive either one concurrently (a character in two encounters at once would race
transcript / reflection writes). :class:`kokoro_link.contracts.pair_lease.PairLeasePort`
is that atomic two-row guard; this module is the runner-facing async context
manager over it — the pair-lease analogue of
:class:`StudioLeaseSession`.

* On enter it acquires the pair lease; a ``None`` handle (either character already
  held by a live lease of a different holder) degrades the session to *not
  acquired* — the handler defers the pair, never half-runs it.
* A background heartbeat renews the lease every ``ttl/3`` so a multi-call LLM run
  never holds a DB transaction open across a model round-trip. A failed renewal
  (expired / taken over) sets ``lost`` so :meth:`raise_if_lost` can abort the run
  cooperatively at a stage boundary.
* On exit it cancels the heartbeat and releases only when the lease is still held
  (a lost lease already belongs to the reclaiming holder — releasing under a
  superseded token is a no-op anyway, so it never touches the new owner's row).

A ``None`` lease → null session (always acquired, no heartbeat, no release): the
self-host / lease-less path is byte-identical to the historical single-process
behaviour where a pair lease is unnecessary.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.pair_lease import PairLeaseHandle, PairLeasePort

_LOGGER = logging.getLogger(__name__)

_DEFAULT_LEASE_TTL_SECONDS = 120
"""Lease window. Wide enough that a legitimately busy encounter (beats + several
dialogue turns + reflect, each an LLM call between heartbeats) never loses its
lease, yet short enough that a genuinely stalled worker's pair is reclaimable
within a couple of minutes. Mirrors :data:`StudioExecutionLease` sizing."""

LEASE_ABANDONED = "lease_abandoned"
"""Handler return sentinel: the run was abandoned because the pair lease is held
by / was reclaimed by another worker. The job must NOT be finalized with visible
output on this outcome — the reclaiming holder owns both characters, and a losing
writer must never race it (mirrors the studio LEASE_ABANDONED contract)."""


class EncounterPairLeaseLost(Exception):
    """Raised at a stage boundary when the pair lease was lost mid-run.

    The handler catches it and ABANDONS without writing transcript / reflection /
    relationship deltas — the worker that took the lease is the new owner."""

    def __init__(self, char_a: str, char_b: str) -> None:
        super().__init__(f"encounter pair lease lost for {char_a}+{char_b}")
        self.char_a = char_a
        self.char_b = char_b


class EncounterPairLeaseSession:
    """Async context manager holding a canonical pair's lease for one run.

    A ``None`` lease → null session: always ``acquired``, no heartbeat, no
    release (the self-host single-process path, byte-identical).
    """

    __slots__ = (
        "_lease", "_char_a", "_char_b", "_owner", "_ttl", "_interval",
        "_clock", "_handle", "_acquired", "_lost", "_heartbeat",
    )

    def __init__(
        self,
        lease: PairLeasePort | None,
        char_a: str,
        char_b: str,
        *,
        owner: str,
        ttl_seconds: int = _DEFAULT_LEASE_TTL_SECONDS,
        clock: ClockPort | None = None,
        heartbeat_interval_seconds: float | None = None,
    ) -> None:
        if ttl_seconds <= 0:
            raise ValueError("EncounterPairLeaseSession.ttl_seconds must be > 0")
        self._lease = lease
        self._char_a = char_a
        self._char_b = char_b
        self._owner = owner
        self._ttl = ttl_seconds
        self._interval = heartbeat_interval_seconds
        self._clock = clock
        self._handle: PairLeaseHandle | None = None
        self._acquired = False
        self._lost = False
        self._heartbeat: asyncio.Task | None = None

    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def lost(self) -> bool:
        return self._lost

    def _now(self) -> datetime:
        if self._clock is not None:
            return ensure_utc(self._clock.now())
        return datetime.now(timezone.utc)

    async def __aenter__(self) -> "EncounterPairLeaseSession":
        if self._lease is None:
            self._acquired = True
            return self
        handle = await self._lease.acquire(
            self._char_a, self._char_b, self._owner,
            ttl_seconds=self._ttl, now=self._now(),
        )
        if handle is None:
            self._acquired = False
            return self
        self._handle = handle
        self._acquired = True
        interval = (
            self._interval
            if self._interval is not None
            else max(1.0, self._ttl / 3.0)
        )
        self._heartbeat = asyncio.create_task(
            self._run_heartbeat(interval),
            name=f"encounter-pair-lease-{self._char_a}-{self._char_b}",
        )
        return self

    async def _run_heartbeat(self, interval: float) -> None:
        try:
            while True:
                await asyncio.sleep(interval)
                assert self._lease is not None and self._handle is not None
                ok = await self._lease.heartbeat(self._handle, now=self._now())
                if not ok:
                    self._lost = True
                    return
        except asyncio.CancelledError:
            pass
        except Exception:
            # A renewal transport error makes ownership ambiguous — stop visible
            # work and let the pair be reclaimed rather than risk a split write.
            self._lost = True
            _LOGGER.exception(
                "encounter pair lease heartbeat failed pair=%s+%s",
                self._char_a, self._char_b,
            )

    def raise_if_lost(self) -> None:
        """Cooperative abort hook — call between beats / stages so a stolen lease
        stops the encounter within one bounded step."""
        if self._lost:
            raise EncounterPairLeaseLost(self._char_a, self._char_b)

    async def __aexit__(self, *exc: object) -> bool:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            try:
                await self._heartbeat
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat = None
        # Release only when we still hold it: a lost lease already belongs to the
        # reclaiming worker, and releasing under a superseded token is a no-op —
        # skip it so we never touch the new owner's rows.
        if (
            self._lease is not None
            and self._handle is not None
            and self._acquired
            and not self._lost
        ):
            try:
                await self._lease.release(self._handle)
            except Exception:
                _LOGGER.exception(
                    "encounter pair lease release failed pair=%s+%s",
                    self._char_a, self._char_b,
                )
        return False
