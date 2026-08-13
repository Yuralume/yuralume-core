"""AP3 — the public price-list proxy (client + cache).

The whole point of the surface is that a player can see a price before acting,
so the tests centre on the two ways it may not lie: a partly-unreadable list
still answers with the rows it could read, and an outage answers ``stale``
rather than "these actions are free".
"""

from __future__ import annotations

import httpx
import pytest

from kokoro_link.application.services.cloud_pricing_service import (
    CloudPricingService,
)
from kokoro_link.application.services.cloud_tier_pricing_service import (
    CloudTierPricingService,
)
from kokoro_link.contracts.cloud_action_pricing import (
    ActionPrice,
    ActionPricingUnavailable,
    PublicPricing,
    TierPricing,
)
from kokoro_link.infrastructure.cloud.action_pricing_client import (
    ActionPricingClient,
    TierActionPricingClient,
)


class _MockAsyncClient(httpx.AsyncClient):
    def __init__(self, handler, **kwargs) -> None:
        super().__init__(
            transport=httpx.MockTransport(handler),
            base_url=kwargs["base_url"],
            timeout=kwargs["timeout"],
        )


def _install(monkeypatch, handler) -> None:
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: _MockAsyncClient(handler, **kwargs),
    )


@pytest.mark.asyncio
async def test_client_reads_the_public_endpoint_without_a_credential(
    monkeypatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["has_token"] = "x-yuralume-service-token" in request.headers
        return httpx.Response(200, json={
            "tiers": [
                {
                    "tier_name": "standard",
                    "billing_shape": "action_fixed",
                    "actions": [
                        {
                            "action_key": "chat",
                            "unit": "per_message",
                            "price_cr": "2.5",
                            "overage": False,
                        },
                    ],
                },
            ],
        })

    _install(monkeypatch, handler)
    pricing = await ActionPricingClient(
        base_url="https://user.example",
    ).fetch()

    assert seen["path"] == "/v1/public/pricing"
    assert seen["has_token"] is False
    assert pricing.tiers[0].tier_name == "standard"
    assert pricing.tiers[0].actions[0].price_cr == 2.5


@pytest.mark.asyncio
async def test_unreadable_rows_are_dropped_not_quoted_as_free(
    monkeypatch,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "tiers": [
                {"billing_shape": "action_fixed"},  # no tier name
                {
                    "tier_name": "standard",
                    "billing_shape": "action_fixed",
                    "actions": [
                        {"action_key": "chat", "price_cr": None},
                        {"action_key": "", "price_cr": 1.0},
                        {"action_key": "image_portrait", "price_cr": 8},
                    ],
                },
            ],
        })

    _install(monkeypatch, handler)
    pricing = await ActionPricingClient(
        base_url="https://user.example",
    ).fetch()

    assert len(pricing.tiers) == 1
    assert [a.action_key for a in pricing.tiers[0].actions] == ["image_portrait"]


@pytest.mark.asyncio
async def test_upstream_failure_is_unavailable(monkeypatch) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    _install(monkeypatch, handler)
    with pytest.raises(ActionPricingUnavailable):
        await ActionPricingClient(base_url="https://user.example").fetch()


class _StubClient:
    def __init__(self, *, pricing: PublicPricing | None = None) -> None:
        self.pricing = pricing or PublicPricing()
        self.calls = 0
        self.fail = False

    async def fetch(self) -> PublicPricing:
        self.calls += 1
        if self.fail:
            raise ActionPricingUnavailable("down")
        return self.pricing


@pytest.mark.asyncio
async def test_cache_holds_for_the_ttl_and_refresh_bypasses_it() -> None:
    clock = {"t": 0.0}
    client = _StubClient()
    service = CloudPricingService(
        client=client, ttl_seconds=300.0, time_source=lambda: clock["t"],
    )

    await service.get_pricing()
    await service.get_pricing()
    assert client.calls == 1

    await service.get_pricing(refresh=True)
    assert client.calls == 2

    clock["t"] = 400.0
    await service.get_pricing()
    assert client.calls == 3


@pytest.mark.asyncio
async def test_outage_serves_last_known_good_flagged_stale() -> None:
    client = _StubClient()
    service = CloudPricingService(client=client)

    fresh = await service.get_pricing()
    assert fresh is not None and fresh.stale is False

    client.fail = True
    stale = await service.get_pricing(refresh=True)
    assert stale is not None and stale.stale is True


@pytest.mark.asyncio
async def test_outage_with_nothing_cached_answers_unknown() -> None:
    client = _StubClient()
    client.fail = True
    service = CloudPricingService(client=client)

    assert await service.get_pricing() is None


# -- the quote the charge path binds to (C1) ---------------------------


def _pricing() -> PublicPricing:
    return PublicPricing(
        tiers=(
            TierPricing(
                tier_name="standard",
                billing_shape="action_fixed",
                actions=(
                    ActionPrice(
                        action_key="chat", unit="per_message", price_cr=2.5,
                    ),
                ),
            ),
            TierPricing(
                tier_name="pro",
                billing_shape="action_fixed",
                actions=(
                    ActionPrice(
                        action_key="chat", unit="per_message", price_cr=2.5,
                    ),
                ),
            ),
        ),
    )


@pytest.mark.asyncio
async def test_the_quote_is_read_from_the_cache_without_fetching() -> None:
    """The binding number must be the one this process actually served."""
    client = _StubClient(pricing=_pricing())
    service = CloudPricingService(client=client)

    assert service.quoted_price_cr(tier_name="standard", action_key="chat") is None

    await service.get_pricing()
    assert client.calls == 1
    assert service.quoted_price_cr(tier_name="standard", action_key="chat") == 2.5
    assert client.calls == 1  # no extra round-trip on the charge path


@pytest.mark.asyncio
async def test_an_unpublished_tier_falls_back_only_on_a_unanimous_price() -> None:
    """Same rule as the SPA's price hint, or the quote would disagree with it."""
    service = CloudPricingService(client=_StubClient(pricing=_pricing()))
    await service.get_pricing()

    assert service.quoted_price_cr(tier_name="renamed", action_key="chat") == 2.5
    assert service.quoted_price_cr(
        tier_name="standard", action_key="image_portrait",
    ) is None


@pytest.mark.asyncio
async def test_warm_fetches_only_when_nothing_is_cached() -> None:
    """The charge path warms a cold process once; a served list stays put."""
    client = _StubClient(pricing=_pricing())
    service = CloudPricingService(client=client)

    await service.warm()
    assert client.calls == 1
    assert service.quoted_price_cr(tier_name="standard", action_key="chat") == 2.5

    await service.warm()
    assert client.calls == 1  # the cached list is what the SPA was served


@pytest.mark.asyncio
async def test_invalidate_drops_the_quote_the_user_service_refused() -> None:
    client = _StubClient(pricing=_pricing())
    service = CloudPricingService(client=client)
    await service.get_pricing()

    service.invalidate()

    assert service.quoted_price_cr(tier_name="standard", action_key="chat") is None
    await service.get_pricing()
    assert client.calls == 2  # refetched rather than serving the stale list


@pytest.mark.asyncio
async def test_private_tier_client_reads_unlisted_price_with_internal_auth(
    monkeypatch,
) -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["tier"] = request.url.params.get("tier")
        seen["token"] = request.headers.get("x-internal-token")
        return httpx.Response(200, json={
            "tiers": [{
                "tier_name": "internal_test",
                "billing_shape": "action_fixed",
                "actions": [{
                    "action_key": "chat",
                    "unit": "per_message",
                    "price_cr": 4,
                    "overage": False,
                }],
            }],
        })

    _install(monkeypatch, handler)
    pricing = await TierActionPricingClient(
        base_url="https://user.example", internal_token="runtime-secret",
    ).fetch("internal_test")

    assert seen == {
        "path": "/internal/v1/runtime-config/action-pricing",
        "tier": "internal_test",
        "token": "runtime-secret",
    }
    assert pricing.tiers[0].actions[0].price_cr == 4


class _StubTierClient:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.fail = False

    async def fetch(self, tier_name: str) -> PublicPricing:
        self.calls.append(tier_name)
        if self.fail:
            raise ActionPricingUnavailable("down")
        price = 4.0 if tier_name == "internal_test" else 6.0
        return PublicPricing(tiers=(TierPricing(
            tier_name=tier_name,
            billing_shape="action_fixed",
            actions=(ActionPrice(
                action_key="chat", unit="per_message", price_cr=price,
            ),),
        ),))


@pytest.mark.asyncio
async def test_private_tier_cache_never_substitutes_the_public_tier() -> None:
    client = _StubTierClient()
    service = CloudTierPricingService(client=client)

    internal = await service.get_pricing("internal_test")
    general = await service.get_pricing("general")

    assert internal is not None
    assert internal.pricing.tiers[0].actions[0].price_cr == 4
    assert general is not None
    assert general.pricing.tiers[0].actions[0].price_cr == 6
    assert client.calls == ["internal_test", "general"]


@pytest.mark.asyncio
async def test_private_tier_cache_resolves_one_action_quote() -> None:
    client = _StubTierClient()
    service = CloudTierPricingService(client=client)

    price = await service.quote_price_cr(
        tier_name="internal_test", action_key="chat",
    )

    assert price == 4.0
    assert client.calls == ["internal_test"]
