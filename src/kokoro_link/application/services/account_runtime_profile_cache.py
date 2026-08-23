"""Cheap account-runtime-profile resolution for the due-job cluster (NF4).

Before NF4 the runtime profile was read only where a knob multiplier actually
scaled a cadence: ``KnobGate.NONE`` kinds (``beat_due`` / ``memorialize`` /
``character_upkeep``) never touched it, and the reconciler's ten computations
per character each paid at most one read. Dormancy changed the shape — it
covers the un-gated kinds too, so *every* computation now needs the profile,
and :class:`AccountRuntimeProfileResolver` sits directly on an uncached
``OperatorProfileRepositoryPort.get``. Left alone that is one DB round-trip
per (character, kind): a 20k-dormant-character reconcile pass turns into
200k reads whose answers are, for one operator, all the same row; and a
healthy account's 300s ``beat_due`` self-chain adds ~288 reads per character
per day that pre-NF4 cost nothing.

Two collaborators, deliberately separate because they bound different things:

* :class:`CachedAccountRuntimeProfileResolver` — a TTL memo *in front of* the
  real resolver, so it is a drop-in ``AccountRuntimeProfileResolverPort``.
  It bounds the **steady-state** rate: whatever the traffic, one operator
  costs at most one read per TTL. Staleness bound = the TTL (default 60 s):
  a tier change reaches background scheduling within a minute, against a
  reconcile cadence of 15 minutes and dormancy windows measured in days.
* :class:`ProfileResolutionScope` — a memo for **one reconcile pass**, so the
  same operator is resolved exactly once no matter how many characters and
  kinds that pass walks. It exists *in addition to* the TTL memo because a
  pass over a large working set can easily outlive any sane TTL, and "once
  per pass" is the property the reconciler actually needs. Staleness bound =
  the duration of one pass, which by construction is at most one reconcile
  interval.

Both memoise *failures* as well as successes within their window: a control
plane that is down must not be hammered ten times per character, and the
fail-open answer (``None`` → "not dormant") is stable for the whole pass.
"""

from __future__ import annotations

import logging
import time
from collections import OrderedDict
from typing import Callable

from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)

_LOGGER = logging.getLogger(__name__)

#: Default TTL of the steady-state memo. Short enough that a control-plane
#: tier change is not meaningfully delayed (the consumers run on 300 s–24 h
#: cadences), long enough that a burst of computations for one operator
#: collapses onto a single read.
DEFAULT_PROFILE_CACHE_TTL_SECONDS = 60.0

#: Bounds the memo's memory on a deployment with many operators. Eviction is
#: least-recently-resolved: a pass sweeping every operator degrades to the
#: uncached rate rather than growing without limit.
DEFAULT_PROFILE_CACHE_MAX_ENTRIES = 8192


async def resolve_profile_or_none(
    resolver: AccountRuntimeProfileResolverPort, operator_id: str,
) -> AccountRuntimeProfile | None:
    """Resolve fail-open: any resolver failure answers ``None``.

    ``None`` means "no policy known", which every caller in this cluster
    reads as "no dormancy, no multiplier" — the cost of a wrong ``None`` is
    one extra cadence, the cost of pretending a blip is a policy would be a
    deployment that silently stops doing background work."""
    try:
        return await resolver.resolve_for_operator(operator_id)
    except Exception:
        _LOGGER.exception(
            "account runtime profile resolve failed operator=%s", operator_id,
        )
        return None


class CachedAccountRuntimeProfileResolver:
    """TTL memo implementing :class:`AccountRuntimeProfileResolverPort`.

    Wraps the real resolver so consumers need no cache-awareness at all —
    the due-job calculator keeps calling ``resolve_for_operator`` and simply
    stops paying a DB round-trip for every call.

    The clock is :func:`time.monotonic` (injectable for tests) rather than a
    wall clock: a clock jump must not be able to extend an entry's life.
    Failures are NOT cached here — they raise through to the caller's own
    fail-open handling, so a recovered control plane is picked up on the very
    next call instead of being pinned to a stale error for a TTL.
    """

    __slots__ = ("_resolver", "_ttl", "_time", "_max_entries", "_entries")

    def __init__(
        self,
        resolver: AccountRuntimeProfileResolverPort,
        *,
        ttl_seconds: float = DEFAULT_PROFILE_CACHE_TTL_SECONDS,
        time_source: Callable[[], float] | None = None,
        max_entries: int = DEFAULT_PROFILE_CACHE_MAX_ENTRIES,
    ) -> None:
        self._resolver = resolver
        self._ttl = max(0.0, float(ttl_seconds))
        self._time = time_source or time.monotonic
        self._max_entries = max(1, int(max_entries))
        #: operator -> (expires_at, profile), in least-recently-resolved order.
        self._entries: OrderedDict[str, tuple[float, AccountRuntimeProfile]] = (
            OrderedDict()
        )

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        if self._ttl <= 0.0:  # cache disabled — pass straight through
            return await self._resolver.resolve_for_operator(operator_id)
        now = self._time()
        cached = self._entries.get(operator_id)
        if cached is not None:
            expires_at, profile = cached
            if expires_at > now:
                self._entries.move_to_end(operator_id)
                return profile
            del self._entries[operator_id]
        profile = await self._resolver.resolve_for_operator(operator_id)
        self._entries[operator_id] = (now + self._ttl, profile)
        self._entries.move_to_end(operator_id)
        self._evict()
        return profile

    def invalidate(self, operator_id: str) -> None:
        """Drop one operator's memo (a policy change this process knows about)."""
        self._entries.pop(operator_id, None)

    def clear(self) -> None:
        self._entries.clear()

    def _evict(self) -> None:
        while len(self._entries) > self._max_entries:
            self._entries.popitem(last=False)


class ProfileResolutionScope:
    """One reconcile pass's memo: an operator is resolved exactly once.

    A pass walks every active character × every kind, and a single operator
    owns many characters, so without this the same answer is re-derived
    dozens of times inside one pass. Unlike the TTL memo this never expires
    mid-pass — "once per pass" is the guarantee, and a pass is bounded by the
    reconcile interval, which is the staleness the reconciler already accepts
    between passes.

    Failures are memoised as ``None`` for the rest of the pass: a control
    plane that is down answers "no policy known" consistently instead of
    being re-hammered once per character.
    """

    __slots__ = ("_resolver", "_entries")

    def __init__(self, resolver: AccountRuntimeProfileResolverPort) -> None:
        self._resolver = resolver
        self._entries: dict[str, AccountRuntimeProfile | None] = {}

    async def resolve(self, operator_id: str) -> AccountRuntimeProfile | None:
        if operator_id in self._entries:
            return self._entries[operator_id]
        profile = await resolve_profile_or_none(self._resolver, operator_id)
        self._entries[operator_id] = profile
        return profile
