"""Per-target execution lease for Creator Studio pipelines.

Phase 4 前置 1.

Fusion / branching generation runs as ``asyncio`` tasks *inside the API
process*, serialized per target by a process-local ``asyncio.Lock``. That lock
cannot span processes: with the ``api`` role scaled to N replicas, two replicas'
startup recovery — or a live create/iterate on one replica racing recovery on
another — would both drive the same story/drama, burning duplicate LLM passes
and racing writes to the same row.

This module adds a *per-target* TTL lease layered on top of the existing lock:

* :class:`StudioExecutionLease` wraps a :class:`BackgroundCoordinatorLeasePort`
  (the same TTL + monotonic-fencing-epoch contract the background coordinator
  uses) under the well-known lease name ``studio:<target_id>``. The SA adapter
  reuses the ``background_runtime_leases`` table; the in-memory adapter is the
  same port's in-process implementation, so a single self-host process behaves
  byte-identically to the historical lock-only path — the lease is always free
  when the lock lets the runner in, so ``acquire`` always succeeds.
* :class:`StudioLeaseSession` is the runner-facing async context manager: it
  acquires on enter, renews on a background heartbeat every ``ttl/3``, exposes
  ``raise_if_lost`` for cooperative abort between pipeline stages, and releases
  on exit. A ``None`` lease degrades it to a null session (always acquired, no
  heartbeat, no release) so lease-less test rigs and the pre-Phase-4 wiring keep
  their exact behaviour.

Ownership is per replica: one ``owner_id`` per process, shared by that process's
fusion service, branching service and recovery service, so a same-process
re-acquire renews the SAME epoch. Recovery can therefore claim a target and hand
off to the runner it spawns — the runner renews the one lease and releases it.
A *different* replica taking over only ever happens after the TTL lapses (this
process stalled), which bumps the epoch and makes our heartbeat renewal return
a mismatched value → cooperative abort. On abort the runner ABANDONS without
writing story/drama or job state, mirroring the P3-C execution runner: the
reclaiming owner's writes must never be raced.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from datetime import datetime, timezone

from kokoro_link.application.services.runtime_claim import LEASE_NAME_MAX_LENGTH
from kokoro_link.contracts.background_jobs import BackgroundCoordinatorLeasePort


_LOGGER = logging.getLogger(__name__)

_DEFAULT_LEASE_TTL_SECONDS = 120
"""Lease window. Wide enough that a legitimately busy pipeline (several LLM
calls / image generations between heartbeats) never loses its lease, yet short
enough that a genuinely stalled replica's target is reclaimable within a couple
of minutes."""

_LEASE_NAME_PREFIX = "studio:"

LEASE_ABANDONED = "lease_abandoned"
"""Runner return sentinel: the run was abandoned because the per-target lease is
held by / was reclaimed by another replica. The tracked-job wrapper must NOT
finalize the job on this outcome — the lease owner finalizes both target and
job, and a losing writer must never race it."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class StudioLeaseLost(Exception):
    """Raised at a stage boundary when the per-target lease was stolen.

    The runner catches it and ABANDONS without writing story/drama or job
    state — the replica that took the lease is the new owner and will finalize
    the target; a losing writer must never race it."""

    def __init__(self, target_id: str) -> None:
        super().__init__(f"studio lease lost for target {target_id}")
        self.target_id = target_id


class StudioExecutionLease:
    """Per-target TTL lease over a :class:`BackgroundCoordinatorLeasePort`.

    ``owner_id`` identifies this replica; it is stable for the process so a
    same-process re-acquire over a still-live lease renews the same epoch (the
    recovery → runner hand-off), while a cross-replica takeover after TTL
    expiry bumps the epoch and is detectable on the next heartbeat.
    """

    __slots__ = ("_lease", "_owner_id", "_ttl", "_prefix")

    def __init__(
        self,
        lease: BackgroundCoordinatorLeasePort,
        *,
        owner_id: str,
        ttl_seconds: int = _DEFAULT_LEASE_TTL_SECONDS,
        name_prefix: str = _LEASE_NAME_PREFIX,
    ) -> None:
        if not owner_id:
            raise ValueError("StudioExecutionLease.owner_id must be non-empty")
        if ttl_seconds <= 0:
            raise ValueError("StudioExecutionLease.ttl_seconds must be > 0")
        if not name_prefix:
            raise ValueError("StudioExecutionLease.name_prefix must be non-empty")
        self._lease = lease
        self._owner_id = owner_id
        self._ttl = ttl_seconds
        self._prefix = name_prefix

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    @property
    def owner_id(self) -> str:
        return self._owner_id

    def _name(self, target_id: str) -> str:
        """Lease row name for ``target_id``, always within the column bound.

        ``BackgroundRuntimeLeaseRow.name`` is ``String(64)``: SQLite stores an
        overlong name silently while PostgreSQL rejects it — i.e. an unclamped
        composite target id (``story_event:{uuid}:{date}`` is 66 chars under
        the ``studio:`` prefix) would pass every unit test and then fail
        exactly where the claim matters. Short names keep their readable form
        byte-identically; overlong ones fall back to a stable digest of the
        readable form, so the key is identical across replicas and restarts.
        """
        name = self._prefix + target_id
        if len(name) <= LEASE_NAME_MAX_LENGTH:
            return name
        digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:32]
        return f"{self._prefix}h:{digest}"

    @property
    def port(self) -> BackgroundCoordinatorLeasePort:
        """The lease backend this instance holds.

        Exposed so a sibling subsystem can layer its OWN owner / TTL /
        namespace policy on the SAME storage without the container having to
        re-decide postgres-vs-in-process (see ``chat_turn_lease``). Reusing the
        backend keeps one lease table; reusing the *instance* would also share
        this process's ``owner_id``, which is exactly wrong for a lease that
        must exclude same-process siblings.
        """
        return self._lease

    async def acquire(
        self, target_id: str, *, now: datetime | None = None,
    ) -> int | None:
        """Acquire the target's lease. Returns the fencing epoch, or ``None``
        when a live lease is held by a *different* replica."""
        return await self._lease.acquire(
            self._name(target_id),
            self._owner_id,
            ttl_seconds=self._ttl,
            now=now or _utcnow(),
        )

    async def heartbeat(
        self, target_id: str, epoch: int, *, now: datetime | None = None,
    ) -> bool:
        """Renew the lease. ``False`` when the lease was lost — the caller is no
        longer the owner, the lease expired, or the epoch was superseded by a
        takeover (renew reports a different epoch than the one we hold)."""
        renewed = await self._lease.renew(
            self._name(target_id),
            self._owner_id,
            ttl_seconds=self._ttl,
            now=now or _utcnow(),
        )
        return renewed is not None and renewed == epoch

    async def release(self, target_id: str) -> None:
        await self._lease.release(self._name(target_id), self._owner_id)


class StudioLeaseSession:
    """Async context manager holding a target's lease for one pipeline run.

    A ``None`` lease → null session: always ``acquired``, no heartbeat, no
    release (the historical lock-only path, byte-identical for self-host).
    """

    __slots__ = (
        "_lease", "_target_id", "_interval", "_max_lifetime",
        "_epoch", "_acquired", "_lost", "_heartbeat",
    )

    def __init__(
        self,
        lease: StudioExecutionLease | None,
        target_id: str,
        *,
        heartbeat_interval_seconds: float | None = None,
        max_lifetime_seconds: float | None = None,
    ) -> None:
        self._lease = lease
        self._target_id = target_id
        self._interval = heartbeat_interval_seconds
        # Optional cap on how long the heartbeat keeps renewing. ``None`` (the
        # default, and every pre-existing caller) renews for as long as the
        # session is open — correct when the session's lifetime is bounded by a
        # single ``async with``. Callers whose session is handed across an
        # await boundary — where a dropped owner could otherwise renew a
        # forgotten lease forever — set this so the lease always decays back to
        # its TTL.
        self._max_lifetime = max_lifetime_seconds
        self._epoch: int | None = None
        self._acquired = False
        self._lost = False
        self._heartbeat: asyncio.Task | None = None

    @property
    def acquired(self) -> bool:
        return self._acquired

    @property
    def lost(self) -> bool:
        return self._lost

    async def __aenter__(self) -> "StudioLeaseSession":
        if self._lease is None:
            self._acquired = True
            return self
        epoch = await self._lease.acquire(self._target_id)
        if epoch is None:
            self._acquired = False
            return self
        self._epoch = epoch
        self._acquired = True
        interval = (
            self._interval
            if self._interval is not None
            else max(1.0, self._lease.ttl_seconds / 3.0)
        )
        self._heartbeat = asyncio.create_task(
            self._run_heartbeat(interval),
            name=f"studio-lease-{self._target_id}",
        )
        return self

    async def _run_heartbeat(self, interval: float) -> None:
        deadline = (
            None if self._max_lifetime is None
            else time.monotonic() + self._max_lifetime
        )
        try:
            while True:
                await asyncio.sleep(interval)
                if deadline is not None and time.monotonic() >= deadline:
                    # Stop renewing without declaring the lease lost: we still
                    # hold it, and the owner-checked release on exit stays
                    # correct. From here the lease simply ages out at its TTL,
                    # so an owner that never releases cannot pin the target.
                    _LOGGER.warning(
                        "lease heartbeat hit max lifetime, letting it expire "
                        "target=%s", self._target_id,
                    )
                    return
                assert self._lease is not None and self._epoch is not None
                ok = await self._lease.heartbeat(self._target_id, self._epoch)
                if not ok:
                    self._lost = True
                    return
        except asyncio.CancelledError:
            pass
        except Exception:
            # A renewal transport error makes ownership ambiguous — stop visible
            # work and let the target be reclaimed rather than risk a split write.
            self._lost = True
            _LOGGER.exception(
                "studio lease heartbeat failed target=%s", self._target_id,
            )

    def raise_if_lost(self) -> None:
        """Cooperative abort hook — call between stages / beats so a stolen
        lease stops the pipeline within one bounded step."""
        if self._lost:
            raise StudioLeaseLost(self._target_id)

    async def __aexit__(self, *exc: object) -> bool:
        if self._heartbeat is not None:
            self._heartbeat.cancel()
            try:
                await self._heartbeat
            except (asyncio.CancelledError, Exception):
                pass
            self._heartbeat = None
        # Release only when we still hold it: a lost lease already belongs to the
        # reclaiming replica, and releasing under a superseded owner_id is a no-op
        # anyway — skip it so we never touch the new owner's row.
        if self._lease is not None and self._acquired and not self._lost:
            try:
                await self._lease.release(self._target_id)
            except Exception:
                _LOGGER.exception(
                    "studio lease release failed target=%s", self._target_id,
                )
        return False
