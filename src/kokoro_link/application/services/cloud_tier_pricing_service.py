"""Per-tier hosted price cache for authenticated player quotes."""

from __future__ import annotations

import time
from collections.abc import Callable

from kokoro_link.application.services.cloud_pricing_service import PricingSnapshot
from kokoro_link.contracts.cloud_action_pricing import (
    ActionPricingUnavailable,
    PublicPricing,
    TierActionPricingPort,
)


class CloudTierPricingService:
    def __init__(
        self, *, client: TierActionPricingPort, ttl_seconds: float = 300.0,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = max(1.0, ttl_seconds)
        self._now = time_source
        self._cached: dict[str, PublicPricing] = {}
        self._fetched_at: dict[str, float] = {}

    async def get_pricing(
        self, tier_name: str, *, refresh: bool = False,
    ) -> PricingSnapshot | None:
        tier = (tier_name or "").strip().casefold()
        cached = self._cached.get(tier)
        fetched_at = self._fetched_at.get(tier, 0.0)
        if (
            not refresh and cached is not None
            and (self._now() - fetched_at) <= self._ttl
        ):
            return PricingSnapshot(pricing=cached, stale=False)
        try:
            pricing = await self._client.fetch(tier)
        except ActionPricingUnavailable:
            if cached is None:
                return None
            return PricingSnapshot(pricing=cached, stale=True)
        self._cached[tier] = pricing
        self._fetched_at[tier] = self._now()
        return PricingSnapshot(pricing=pricing, stale=False)

    async def quote_price_cr(
        self, *, tier_name: str, action_key: str,
    ) -> float | None:
        """Resolve a server-side quote without crossing tier boundaries."""
        tier = (tier_name or "").strip().casefold()
        action = (action_key or "").strip()
        if not tier or not action:
            return None
        snapshot = await self.get_pricing(tier)
        if snapshot is None:
            return None
        return _exact_tier_action_price(
            snapshot.pricing, tier_name=tier, action_key=action,
        )

    def invalidate(self, tier_name: str) -> None:
        tier = (tier_name or "").strip().casefold()
        if not tier:
            return
        self._cached.pop(tier, None)
        self._fetched_at.pop(tier, None)


def _exact_tier_action_price(
    pricing: PublicPricing, *, tier_name: str, action_key: str,
) -> float | None:
    wanted_tier = (tier_name or "").strip().casefold()
    wanted_action = (action_key or "").strip()
    for tier in pricing.tiers:
        if (tier.tier_name or "").strip().casefold() != wanted_tier:
            continue
        for action in tier.actions:
            if action.action_key == wanted_action:
                return action.price_cr
        return None
    return None
