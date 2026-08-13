"""HTTP client for the User service's public price list (AP3).

The one Cloud endpoint Core calls with **no credential**: the price list is
deliberately public (it is the disclosure the terms of service promise), so
sending a service token here would only widen the credential's blast radius
for nothing.

Parsing is tolerant by row and strict by field: a tier or action entry that
cannot be read is dropped rather than voiding the whole list, because a price
list missing one row still answers the player's question, while a hard failure
answers nothing. A row that *is* kept always carries a real number — never a
coerced ``0``, which would quote an action as free.
"""

from __future__ import annotations

from math import isfinite

import httpx

from kokoro_link.contracts.cloud_action_pricing import (
    ActionPrice,
    ActionPricingPort,
    ActionPricingUnavailable,
    PublicPricing,
    TierActionPricingPort,
    TierPricing,
)
from kokoro_link.infrastructure.cloud.internal_service_auth import outbound_headers

_PATH = "/v1/public/pricing"


class ActionPricingClient(ActionPricingPort):
    def __init__(
        self, *, base_url: str, timeout_seconds: float = 5.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds

    async def fetch(self) -> PublicPricing:
        if not self._base_url:
            raise ActionPricingUnavailable("cloud user service URL is empty")
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout_seconds,
            ) as client:
                response = await client.get(_PATH)
        except httpx.HTTPError as exc:
            raise ActionPricingUnavailable(
                "cloud user service unavailable",
            ) from exc
        if response.status_code >= 400:
            raise ActionPricingUnavailable(
                f"cloud user service returned {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ActionPricingUnavailable(
                "pricing response is not valid JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise ActionPricingUnavailable(
                "pricing response is not a JSON object",
            )
        raw_tiers = payload.get("tiers")
        if not isinstance(raw_tiers, list):
            raise ActionPricingUnavailable(
                "pricing response carries no tier list",
            )
        return PublicPricing(
            tiers=tuple(
                tier for tier in (_parse_tier(row) for row in raw_tiers)
                if tier is not None
            ),
        )


class TierActionPricingClient(TierActionPricingPort):
    """Authenticated private read for one active, possibly unlisted tier."""

    _PATH = "/internal/v1/runtime-config/action-pricing"

    def __init__(
        self, *, base_url: str, timeout_seconds: float = 5.0,
        internal_token: str = "", internal_credential: str = "",
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = timeout_seconds
        self._internal_token = (internal_token or "").strip()
        self._internal_credential = (internal_credential or "").strip()

    async def fetch(self, tier_name: str) -> PublicPricing:
        cleaned = (tier_name or "").strip()
        if not self._base_url or not cleaned:
            raise ActionPricingUnavailable(
                "cloud user service URL or tier is empty",
            )
        headers = outbound_headers(
            self._internal_credential, legacy_token=self._internal_token,
        )
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=self._timeout_seconds,
            ) as client:
                response = await client.get(
                    self._PATH, params={"tier": cleaned}, headers=headers,
                )
        except httpx.HTTPError as exc:
            raise ActionPricingUnavailable(
                "cloud control-plane unavailable",
            ) from exc
        if response.status_code >= 400:
            raise ActionPricingUnavailable(
                f"cloud control-plane returned {response.status_code}",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ActionPricingUnavailable(
                "pricing response is not valid JSON",
            ) from exc
        if not isinstance(payload, dict):
            raise ActionPricingUnavailable(
                "pricing response is not a JSON object",
            )
        raw_tiers = payload.get("tiers")
        if not isinstance(raw_tiers, list):
            raise ActionPricingUnavailable(
                "pricing response carries no tier list",
            )
        return PublicPricing(
            tiers=tuple(
                tier for tier in (_parse_tier(row) for row in raw_tiers)
                if tier is not None
            ),
        )


def _parse_tier(row: object) -> TierPricing | None:
    if not isinstance(row, dict):
        return None
    tier_name = str(row.get("tier_name") or "").strip()
    if not tier_name:
        return None
    raw_actions = row.get("actions")
    actions = raw_actions if isinstance(raw_actions, list) else []
    return TierPricing(
        tier_name=tier_name,
        billing_shape=str(row.get("billing_shape") or "").strip(),
        actions=tuple(
            action for action in (_parse_action(item) for item in actions)
            if action is not None
        ),
    )


def _parse_action(row: object) -> ActionPrice | None:
    if not isinstance(row, dict):
        return None
    action_key = str(row.get("action_key") or "").strip()
    price = _amount(row.get("price_cr"))
    if not action_key or price is None:
        return None
    return ActionPrice(
        action_key=action_key,
        unit=str(row.get("unit") or "").strip(),
        price_cr=price,
        overage=row.get("overage") is True,
    )


def _amount(raw: object) -> float | None:
    if isinstance(raw, bool) or raw is None:
        return None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        try:
            value = float(raw.strip())
        except ValueError:
            return None
    else:
        return None
    return value if isfinite(value) and value >= 0 else None
