"""In-process per-tier cache for control-plane runtime profiles.

Keeps ``AccountRuntimeProfile`` resolution off the network on the hot path: a
warm, unexpired tier returns synchronously; a cold / stale tier fetches once.

Unlike the routing-profile cache this resolver **never raises out of
``fetch``** (plan H2 §8): a control-plane outage serves the last-known-good
profile for that tier if one was ever cached, else ``None`` — the account
runtime-profile resolver treats ``None`` as "fall back to the default policy",
so a paying tenant is never hard-failed by a transient outage.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from kokoro_link.contracts.cloud_tier_runtime_profile import (
    TierRuntimeProfilePort,
    TierRuntimeProfileUnavailable,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)

_DEFAULT_TTL_SECONDS = 300.0


class CachedTierRuntimeProfileResolver(TierRuntimeProfilePort):
    def __init__(
        self,
        *,
        client: TierRuntimeProfilePort,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = max(1.0, ttl_seconds)
        self._now = time_source
        # ``None`` is a legitimate cached value (a tier with no control-plane
        # profile → 404); it is served as last-known-good just like a profile.
        self._cache: dict[str, AccountRuntimeProfile | None] = {}
        self._fetched_at: dict[str, float] = {}

    async def fetch(self, tier: str) -> AccountRuntimeProfile | None:
        key = (tier or "").strip()
        if key in self._cache and not self._expired(key):
            return self._cache[key]
        try:
            profile = await self._client.fetch(key)
        except TierRuntimeProfileUnavailable:
            # Serve last-known-good if we ever cached this tier, else None.
            # Never propagate: the resolver must not hard-fail on an outage.
            return self._cache.get(key)
        self._store(key, profile)
        return profile

    def _expired(self, key: str) -> bool:
        return (self._now() - self._fetched_at.get(key, 0.0)) > self._ttl

    def _store(self, key: str, profile: AccountRuntimeProfile | None) -> None:
        self._cache[key] = profile
        self._fetched_at[key] = self._now()
