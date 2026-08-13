"""Core-side views of hosted action pricing (AP3).

The Cloud control plane owns both the public storefront list and the private
single-tier list Core serves to an authenticated player. The private view is
required because an active unlisted tier can legitimately charge a different
amount from the storefront tier; substituting the public number creates an
unrecoverable ``409 price_changed`` loop.

Core stores no prices and hardcodes none — it only caches snapshots.
Whatever the back office says today is what the player is quoted today.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class ActionPricingUnavailable(RuntimeError):
    """The price list could not be read. Always "we do not know", never
    "these actions are free"."""


@dataclass(frozen=True, slots=True)
class ActionPrice:
    action_key: str
    unit: str
    price_cr: float
    overage: bool = False


@dataclass(frozen=True, slots=True)
class TierPricing:
    tier_name: str
    billing_shape: str
    actions: tuple[ActionPrice, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class PublicPricing:
    tiers: tuple[TierPricing, ...] = field(default_factory=tuple)


class ActionPricingPort(Protocol):
    async def fetch(self) -> PublicPricing:
        """Read the current published price list.

        Raises :class:`ActionPricingUnavailable` on any failure; the caching
        service decides whether to serve last-known-good.
        """


class TierActionPricingPort(Protocol):
    async def fetch(self, tier_name: str) -> PublicPricing:
        """Read one active tier's prices from the private control plane.

        Raises :class:`ActionPricingUnavailable` on any failure. The caller
        must never substitute another tier's public price.
        """


class TierQuotedPricePort(Protocol):
    """Authoritative server-side quote for one exact hosted tier."""

    async def quote_price_cr(
        self, *, tier_name: str, action_key: str,
    ) -> float | None:
        """Return one action's current price, or `None` when unknown.

        Implementations must never substitute another tier's public price.
        """

    def invalidate(self, tier_name: str) -> None:
        """Drop only this tier's cached private quote."""
