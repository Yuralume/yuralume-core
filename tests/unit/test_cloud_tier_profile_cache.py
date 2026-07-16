"""Per-tier TTL cache in front of the control-plane runtime-profile client.

Guarantees: a warm, unexpired tier is served without a client call; an expired
tier refetches; a ``TierRuntimeProfileUnavailable`` outage serves last-known-
good (profile *or* the cached ``None``) and never raises out of ``fetch``.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.cloud_tier_profile_cache import (
    CachedTierRuntimeProfileResolver,
)
from kokoro_link.contracts.cloud_tier_runtime_profile import (
    TierRuntimeProfileUnavailable,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)


class _FakeClient:
    def __init__(self) -> None:
        self.calls = 0
        self.result: AccountRuntimeProfile | None = AccountRuntimeProfile(
            name="plus", max_characters=5,
        )
        self.unavailable = False

    async def fetch(self, tier: str) -> AccountRuntimeProfile | None:
        self.calls += 1
        if self.unavailable:
            raise TierRuntimeProfileUnavailable("control-plane down")
        return self.result


def _resolver(client, clock, *, ttl: float = 300.0):
    return CachedTierRuntimeProfileResolver(
        client=client, ttl_seconds=ttl, time_source=lambda: clock[0],
    )


@pytest.mark.asyncio
async def test_warm_tier_is_served_without_a_second_client_call() -> None:
    client = _FakeClient()
    clock = [0.0]
    resolver = _resolver(client, clock)

    first = await resolver.fetch("plus")
    clock[0] = 100.0  # still inside the 300s TTL
    second = await resolver.fetch("plus")

    assert first is second
    assert first.max_characters == 5
    assert client.calls == 1


@pytest.mark.asyncio
async def test_expired_tier_refetches() -> None:
    client = _FakeClient()
    clock = [0.0]
    resolver = _resolver(client, clock)

    await resolver.fetch("plus")
    clock[0] = 301.0  # past the TTL
    await resolver.fetch("plus")

    assert client.calls == 2


@pytest.mark.asyncio
async def test_outage_serves_last_known_good_profile() -> None:
    client = _FakeClient()
    clock = [0.0]
    resolver = _resolver(client, clock)

    warm = await resolver.fetch("plus")
    clock[0] = 301.0  # force a refetch
    client.unavailable = True
    served = await resolver.fetch("plus")

    assert served is warm  # outage → last-known-good, not an exception


@pytest.mark.asyncio
async def test_outage_without_prior_cache_returns_none() -> None:
    client = _FakeClient()
    client.unavailable = True
    clock = [0.0]
    resolver = _resolver(client, clock)

    # Never raises; no prior cache → None (resolver falls back to default).
    assert await resolver.fetch("plus") is None


@pytest.mark.asyncio
async def test_cached_none_is_last_known_good() -> None:
    client = _FakeClient()
    client.result = None  # tier has no control-plane profile (404)
    clock = [0.0]
    resolver = _resolver(client, clock)

    first = await resolver.fetch("plus")
    second = await resolver.fetch("plus")  # within TTL, served from cache

    assert first is None
    assert second is None
    assert client.calls == 1

    # After expiry, an outage still serves the cached None rather than raising.
    clock[0] = 301.0
    client.unavailable = True
    assert await resolver.fetch("plus") is None
