"""In-process cache for the public hosted price list (AP3).

Same fail-soft shape as :class:`~kokoro_link.application.services.cloud_credit_service.CloudCreditService`,
with a very different TTL: a balance changes with every action (20s), a price
list changes when the owner edits the back office (300s, matching the
``Cache-Control: max-age=300`` the endpoint publishes). An outage serves the
previous list flagged ``stale`` rather than a confident empty one — quoting no
price at all is better than quoting zero.

There is no per-tenant dimension: the list is public and identical for
everyone, so one process-wide slot is the whole cache.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from kokoro_link.contracts.cloud_action_pricing import (
    ActionPricingPort,
    ActionPricingUnavailable,
    PublicPricing,
)
from kokoro_link.domain.value_objects.account_runtime_profile import (
    BILLING_SHAPE_ACTION_FIXED,
)

_DEFAULT_TTL_SECONDS = 300.0


@dataclass(frozen=True, slots=True)
class PricingSnapshot:
    pricing: PublicPricing
    stale: bool
    """True when the upstream read failed and this is last-known-good."""


class CloudPricingService:
    def __init__(
        self,
        *,
        client: ActionPricingPort,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = max(1.0, ttl_seconds)
        self._now = time_source
        self._cached: PublicPricing | None = None
        self._fetched_at = 0.0

    async def get_pricing(
        self, *, refresh: bool = False,
    ) -> PricingSnapshot | None:
        """The current price list, or ``None`` when nothing is known."""
        if not refresh and self._cached is not None and not self._expired():
            return PricingSnapshot(pricing=self._cached, stale=False)
        try:
            pricing = await self._client.fetch()
        except ActionPricingUnavailable:
            cached = self._cached
            if cached is None:
                return None
            return PricingSnapshot(pricing=cached, stale=True)
        self._cached = pricing
        self._fetched_at = self._now()
        return PricingSnapshot(pricing=pricing, stale=False)

    def quoted_price_cr(
        self, *, tier_name: str, action_key: str,
    ) -> float | None:
        """The cached price for one action — what the player was quoted.

        Deliberately synchronous and non-fetching: this is consumed by the
        charge path to bind the charge to the number the SPA was actually
        served, and that number is by definition the cached one. Ignores the
        TTL for the same reason — an expired entry is still the last thing this
        process handed out.
        """
        pricing = self._cached
        if pricing is None:
            return None
        action = (action_key or "").strip()
        if not action:
            return None
        return _resolve_price(
            pricing, tier_name=tier_name, action_key=action,
        )

    def invalidate(self) -> None:
        """Forget the cached list — the published prices are known to have
        moved, so serving it again would re-quote a number the User service has
        already refused."""
        self._cached = None
        self._fetched_at = 0.0

    async def warm(self) -> None:
        """Fetch once so a cold process has a quote to bind charges to."""
        if self._cached is not None:
            return
        await self.get_pricing()

    def _expired(self) -> bool:
        return (self._now() - self._fetched_at) > self._ttl


def _resolve_price(
    pricing: PublicPricing, *, tier_name: str, action_key: str,
) -> float | None:
    """This player's price for one action, or ``None`` without guessing.

    The player's own tier is the answer whenever the list publishes it. When it
    does not (a tier renamed on one side of the wire), the fallback is the
    unanimous fixed-price answer — the rule the SPA's own price hint follows.
    Binding a charge to any *other* number would make the quote disagree with
    what the player was shown, and every such disagreement becomes a spurious
    "the price changed" in their face.
    """
    wanted = (tier_name or "").strip().casefold()
    for tier in pricing.tiers:
        if (tier.tier_name or "").strip().casefold() != wanted:
            continue
        for action in tier.actions:
            if action.action_key == action_key:
                return action.price_cr
        # The player's own tier is published and does not sell this.
        return None
    prices: set[float] = set()
    for tier in pricing.tiers:
        if tier.billing_shape != BILLING_SHAPE_ACTION_FIXED:
            continue
        entry = next(
            (a for a in tier.actions if a.action_key == action_key), None,
        )
        if entry is None:
            return None
        prices.add(entry.price_cr)
    return prices.pop() if len(prices) == 1 else None
