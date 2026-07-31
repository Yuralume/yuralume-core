"""Core-side view of the public hosted price list (AP3).

The price list is owned by the Cloud control plane and published un-authenticated
at ``GET /v1/public/pricing``. Core proxies it for one reason: the in-game SPA
cannot read the User service directly (no CORS there, and the Portal CSP pins
``connect-src 'self'``), and a player must be able to see what an action costs
*before* pressing the button.

Core stores no prices and hardcodes none — it caches a snapshot and forgets it.
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
