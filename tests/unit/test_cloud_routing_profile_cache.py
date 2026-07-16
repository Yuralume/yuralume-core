from __future__ import annotations

import asyncio

import pytest

from kokoro_link.application.services.cloud_routing_profile_cache import (
    CachedCloudRoutingProfileResolver,
)
from kokoro_link.contracts.cloud_routing_profile import (
    CloudRoutingProfile,
    CloudRoutingProfileUnavailable,
)


def _profile(version: int) -> CloudRoutingProfile:
    return CloudRoutingProfile(
        llm_feature_presets={"chat": f"preset-{version}"},
        image_feature_presets={},
        video_feature_presets={},
        tts_voice_defaults={},
        strict_no_fallback=True,
        disabled_features=frozenset(),
        catalog_version=version,
        routing_policy_version=version,
    )


class _CountingClient:
    def __init__(self) -> None:
        self.calls = 0
        self.version = 1
        self.fail = False
        self.scopes: list[tuple[str, str, str, str]] = []

    async def get_profile(
        self, *, tenant_id: str, account_id: str, tier: str, user_id: str = ""
    ) -> CloudRoutingProfile:
        self.calls += 1
        self.scopes.append((tenant_id, account_id, user_id, tier))
        if self.fail:
            raise CloudRoutingProfileUnavailable("control-plane down")
        return _profile(self.version)


@pytest.mark.asyncio
async def test_warm_read_does_not_refetch() -> None:
    client = _CountingClient()
    cache = CachedCloudRoutingProfileResolver(client=client, refresh_interval_seconds=1000)

    first = await cache.get_profile(tenant_id="t", account_id="a", tier="demo")
    second = await cache.get_profile(tenant_id="t", account_id="a", tier="demo")

    assert first.catalog_version == 1
    assert second is first
    assert client.calls == 1  # warm read is O(1), no synchronous control-plane call


@pytest.mark.asyncio
async def test_background_refresh_when_stale() -> None:
    now = {"t": 0.0}
    client = _CountingClient()
    cache = CachedCloudRoutingProfileResolver(
        client=client,
        refresh_interval_seconds=10.0,
        time_source=lambda: now["t"],
    )

    await cache.get_profile(tenant_id="t", account_id="a", tier="demo")
    assert client.calls == 1

    # Advance past the refresh interval and serve a new version on next fetch.
    now["t"] = 100.0
    client.version = 2
    served = await cache.get_profile(tenant_id="t", account_id="a", tier="demo")
    assert served.catalog_version == 1  # still returns last-known-good synchronously

    # Let the scheduled background refresh run.
    for _ in range(5):
        await asyncio.sleep(0)
    assert client.calls == 2
    refreshed = await cache.get_profile(tenant_id="t", account_id="a", tier="demo")
    assert refreshed.catalog_version == 2


@pytest.mark.asyncio
async def test_separate_scopes_are_cached_independently() -> None:
    client = _CountingClient()
    cache = CachedCloudRoutingProfileResolver(client=client, refresh_interval_seconds=1000)

    await cache.get_profile(tenant_id="t", account_id="a1", tier="demo")
    await cache.get_profile(tenant_id="t", account_id="a2", tier="demo")

    assert client.calls == 2


@pytest.mark.asyncio
async def test_user_scope_is_part_of_cache_key_and_forwarded() -> None:
    client = _CountingClient()
    cache = CachedCloudRoutingProfileResolver(client=client, refresh_interval_seconds=1000)

    await cache.get_profile(tenant_id="t", account_id="a", tier="demo", user_id="u1")
    await cache.get_profile(tenant_id="t", account_id="a", tier="demo", user_id="u2")

    # Two users on the same account/tier are cached independently (no cross-user bleed).
    assert client.calls == 2
    assert client.scopes == [("t", "a", "u1", "demo"), ("t", "a", "u2", "demo")]


@pytest.mark.asyncio
async def test_prolonged_outage_fails_closed_after_max_age() -> None:
    now = {"t": 0.0}
    client = _CountingClient()
    cache = CachedCloudRoutingProfileResolver(
        client=client,
        refresh_interval_seconds=10.0,
        max_age_seconds=60.0,
        time_source=lambda: now["t"],
    )

    await cache.get_profile(tenant_id="t", account_id="a", tier="demo")  # warm (v1)
    client.fail = True

    # Within max_age: still serves last-known-good (and schedules a failing refresh).
    now["t"] = 30.0
    served = await cache.get_profile(tenant_id="t", account_id="a", tier="demo")
    assert served.catalog_version == 1
    for _ in range(5):
        await asyncio.sleep(0)

    # Past max_age with refreshes still failing: fail closed instead of serving stale.
    now["t"] = 1000.0
    with pytest.raises(CloudRoutingProfileUnavailable):
        await cache.get_profile(tenant_id="t", account_id="a", tier="demo")
