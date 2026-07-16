"""In-process cache for control-plane routing profiles.

Keeps the chat/generation hot path an O(1) lookup: a warm key returns the cached
profile synchronously and kicks an async background refresh when stale; only a cold
miss awaits the client once (plan §3.2.3).

Last-known-good is bounded (plan §3.4): if refreshes keep failing past
``max_age_seconds``, the cached profile is treated as stale-expired and the next read
fails closed (raises) instead of serving an unbounded-stale profile — mirroring the
Gateway's ``stale-ttl`` behavior.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

from kokoro_link.contracts.cloud_routing_profile import (
    CloudRoutingProfile,
    CloudRoutingProfilePort,
)

_Key = tuple[str, str, str, str]


class CachedCloudRoutingProfileResolver(CloudRoutingProfilePort):
    def __init__(
        self,
        *,
        client: CloudRoutingProfilePort,
        refresh_interval_seconds: float = 20.0,
        max_age_seconds: float | None = 600.0,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._refresh_interval = max(1.0, refresh_interval_seconds)
        # None disables the bound (serve last-known-good forever); a finite value makes
        # a prolonged outage fail closed once the cached profile is older than it.
        self._max_age = max_age_seconds
        self._now = time_source
        self._cache: dict[_Key, CloudRoutingProfile] = {}
        self._fetched_at: dict[_Key, float] = {}  # last SUCCESSFUL fetch
        self._last_attempt: dict[_Key, float] = {}  # last refresh attempt (success or fail)
        self._refreshing: set[_Key] = set()

    async def get_profile(
        self, *, tenant_id: str, account_id: str, tier: str, user_id: str = ""
    ) -> CloudRoutingProfile:
        key = (tenant_id, account_id, user_id, tier)
        cached = self._cache.get(key)
        if cached is not None and not self._expired(key):
            self._maybe_schedule_refresh(key)
            return cached
        # Cold miss, or last-known-good has aged past max_age: fetch synchronously and
        # fail closed (propagate) if the control-plane is unavailable.
        return await self._fetch(key)

    def _expired(self, key: _Key) -> bool:
        if self._max_age is None:
            return False
        return (self._now() - self._fetched_at.get(key, 0.0)) > self._max_age

    async def _fetch(self, key: _Key) -> CloudRoutingProfile:
        tenant_id, account_id, user_id, tier = key
        self._last_attempt[key] = self._now()
        profile = await self._client.get_profile(
            tenant_id=tenant_id, account_id=account_id, user_id=user_id, tier=tier,
        )
        self._store(key, profile)
        return profile

    def _store(self, key: _Key, profile: CloudRoutingProfile) -> None:
        self._cache[key] = profile
        now = self._now()
        self._fetched_at[key] = now
        self._last_attempt[key] = now

    def _maybe_schedule_refresh(self, key: _Key) -> None:
        last = self._last_attempt.get(key, 0.0)
        if self._now() - last < self._refresh_interval:
            return
        if key in self._refreshing:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._refreshing.add(key)
        self._last_attempt[key] = self._now()
        loop.create_task(self._refresh(key))

    async def _refresh(self, key: _Key) -> None:
        tenant_id, account_id, user_id, tier = key
        try:
            profile = await self._client.get_profile(
                tenant_id=tenant_id, account_id=account_id, user_id=user_id, tier=tier,
            )
            self._store(key, profile)
        except Exception:
            # Keep last-known-good but do NOT reset _fetched_at, so it ages out and a
            # prolonged outage eventually fails closed (plan §3.4).
            pass
        finally:
            self._refreshing.discard(key)
