"""Cross-instance single-flight / cooldown claims over the runtime lease table.

Several application services guard "only one of us should do this" with a
process-local ``asyncio.Lock`` or a process-local ``dict`` of timestamps. Under
the hosted topology (2×api + 2×worker) those guards are per replica, so each
replica happily does the work once — burning N× the LLM spend and, when the
work is user-visible, letting the player watch replica A's answer get replaced
by replica B's.

:class:`RuntimeClaim` is the smallest thing that fixes that: a TTL claim on the
shared ``background_runtime_leases`` table (the same
:class:`BackgroundCoordinatorLeasePort` contract the coordinator, Studio and
encounter pair leases already use). Two usage shapes:

* **Single-flight** — acquire, do the work, ``release`` in a ``finally``. The
  losers call :meth:`await_peer` to wait a *bounded* time for the winner's
  result. A winner that crashes mid-work never wedges the key: the TTL lapses
  and the next caller takes over.
* **Cooldown** — acquire and deliberately *never* release. The TTL itself is the
  cooldown window, so "at most once per TTL across the whole fleet" needs no
  extra state and self-heals on expiry.

A ``None`` lease degrades every claim to "always mine, never wait", which is
exactly the historical single-process behaviour — the self-host red line.

Note on ownership: ``acquire`` renews (and therefore succeeds) for the SAME
``owner_id`` over a still-live claim, per the port contract. That is the right
semantic for a single-flight retry, but it means a cooldown claim does not
suppress a *same-process* repeat on its own — pair it with the caller's existing
in-process gate, which is cheap and covers precisely that case.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, TypeVar

from kokoro_link.contracts.background_jobs import BackgroundCoordinatorLeasePort

_LOGGER = logging.getLogger(__name__)

T = TypeVar("T")

LEASE_NAME_MAX_LENGTH = 64
"""``BackgroundRuntimeLeaseRow.name`` is a ``String(64)`` primary key. A naive
``f"{prefix}:{uuid}:{uuid}"`` key is 79 chars — it fits silently on SQLite and
is rejected by PostgreSQL, i.e. it would break exactly where the claim matters."""

_DEFAULT_TTL_SECONDS = 180
_DEFAULT_WAIT_SECONDS = 8.0
_DEFAULT_POLL_INTERVAL_SECONDS = 0.4


def runtime_lease_name(prefix: str, *parts: str) -> str:
    """Build a lease key that always fits :data:`LEASE_NAME_MAX_LENGTH`.

    Keeps the readable ``prefix:part:part`` form while it fits (so an operator
    reading ``background_runtime_leases`` can tell what a row guards) and falls
    back to a stable SHA-256 digest when it does not. The digest is computed
    from the readable form, so it is identical across processes, restarts and
    replicas — the only property a claim key actually needs.
    """
    if not prefix:
        raise ValueError("runtime_lease_name requires a non-empty prefix")
    readable = ":".join((prefix, *parts))
    if len(readable) <= LEASE_NAME_MAX_LENGTH:
        return readable
    digest = hashlib.sha256(readable.encode("utf-8")).hexdigest()[:32]
    return f"{prefix}:h:{digest}"


class RuntimeClaim:
    """A namespaced TTL claim over a :class:`BackgroundCoordinatorLeasePort`.

    ``prefix`` namespaces the key family (``sched``, ``dream``, …); the caller
    passes the identifying parts and never builds the raw lease name itself.
    """

    __slots__ = (
        "_lease", "_prefix", "_owner_id", "_ttl", "_wait", "_poll",
    )

    def __init__(
        self,
        lease: BackgroundCoordinatorLeasePort | None,
        *,
        prefix: str,
        owner_id: str,
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        wait_seconds: float = _DEFAULT_WAIT_SECONDS,
        poll_interval_seconds: float = _DEFAULT_POLL_INTERVAL_SECONDS,
    ) -> None:
        if not prefix:
            raise ValueError("RuntimeClaim.prefix must be non-empty")
        if lease is not None and not owner_id:
            raise ValueError("RuntimeClaim.owner_id must be non-empty")
        if ttl_seconds <= 0:
            raise ValueError("RuntimeClaim.ttl_seconds must be > 0")
        if poll_interval_seconds <= 0:
            raise ValueError("RuntimeClaim.poll_interval_seconds must be > 0")
        self._lease = lease
        self._prefix = prefix
        self._owner_id = owner_id
        self._ttl = ttl_seconds
        self._wait = max(0.0, wait_seconds)
        self._poll = poll_interval_seconds

    @property
    def enabled(self) -> bool:
        """``False`` for the lease-less (self-host) path."""
        return self._lease is not None

    @property
    def ttl_seconds(self) -> int:
        """Default claim lifetime, in seconds.

        Exposed so wiring can be asserted: a TTL shorter than the guarded work
        expires mid-flight and lets a second replica take over, which is exactly
        the duplicate the claim exists to prevent.
        """
        return self._ttl

    async def acquire(
        self,
        *parts: str,
        ttl_seconds: int | None = None,
        now: datetime | None = None,
    ) -> bool:
        """``True`` when this process owns the claim and may do the work.

        A transport failure is treated as *acquired*: the claim is an
        optimisation (avoid duplicate work), never a correctness gate, so a
        degraded lease table must not stop the feature from running.
        """
        if self._lease is None:
            return True
        name = runtime_lease_name(self._prefix, *parts)
        try:
            epoch = await self._lease.acquire(
                name,
                self._owner_id,
                ttl_seconds=ttl_seconds or self._ttl,
                now=now or datetime.now(timezone.utc),
            )
        except Exception:
            _LOGGER.exception("runtime claim acquire failed name=%s", name)
            return True
        return epoch is not None

    async def release(self, *parts: str) -> None:
        """Vacate the claim. Never raises — a failed release just means the
        key stays held until its TTL lapses."""
        if self._lease is None:
            return
        name = runtime_lease_name(self._prefix, *parts)
        try:
            await self._lease.release(name, self._owner_id)
        except Exception:
            _LOGGER.exception("runtime claim release failed name=%s", name)

    async def await_peer(
        self, probe: Callable[[], Awaitable[T | None]],
    ) -> T | None:
        """Poll ``probe`` until it yields a result or the wait budget expires.

        Bounded on purpose: the losing caller may be a user-facing request, so
        it waits a few seconds for the winner's result and otherwise returns
        ``None`` for the caller to degrade gracefully. Never wait for the full
        TTL — that would trade duplicated work for a hung request.
        """
        waited = 0.0
        while waited < self._wait:
            await asyncio.sleep(self._poll)
            waited += self._poll
            try:
                result = await probe()
            except Exception:
                _LOGGER.exception("runtime claim peer probe failed")
                return None
            if result is not None:
                return result
        return None
