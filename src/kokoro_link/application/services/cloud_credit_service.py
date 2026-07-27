"""In-process per-tenant cache for the hosted credit ("螢火") balance.

The badge has to feel *live* — a player who just spent credits expects the next
render to reflect it — so the TTL is short (20s) and the front end forces a
refresh after each deliberate action. That is a much tighter budget than the
tier-profile cache's 300s, which describes near-static configuration.

Two deliberate departures from ``CachedTierRuntimeProfileResolver``:

- **fail-soft is explicit, not silent.** An outage serves the previous value
  flagged ``stale`` so the SPA can dim the badge instead of showing a confident
  wrong number. Never having had a value returns ``None`` (badge hidden).
- **no cross-instance invalidation.** This is display-only data with no
  authorization consequence, so a few seconds of divergence between hosted
  instances is acceptable and no pg_notify channel is warranted.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from kokoro_link.contracts.cloud_credit_balance import (
    CloudCreditBalance,
    CreditBalancePort,
    CreditBalanceUnavailable,
)

_DEFAULT_TTL_SECONDS = 20.0


@dataclass(frozen=True, slots=True)
class CloudCreditSnapshot:
    """A balance plus how much it can be trusted."""

    gift_cr: float
    purchased_cr: float
    total_cr: float
    stale: bool
    """True when the upstream read failed and this is last-known-good."""


class CloudCreditService:
    def __init__(
        self,
        *,
        client: CreditBalancePort,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._ttl = max(1.0, ttl_seconds)
        self._now = time_source
        self._cache: dict[str, CloudCreditBalance] = {}
        self._fetched_at: dict[str, float] = {}

    async def get_balance(
        self, tenant_id: str, *, refresh: bool = False,
    ) -> CloudCreditSnapshot | None:
        """Return the tenant's balance, or ``None`` when nothing is known.

        ``refresh`` bypasses a warm cache — used right after a player action so
        the badge settles on the post-charge number within a second or two."""
        key = (tenant_id or "").strip()
        if not key:
            return None
        if not refresh and key in self._cache and not self._expired(key):
            return _snapshot(self._cache[key], stale=False)
        try:
            balance = await self._client.fetch(key)
        except CreditBalanceUnavailable:
            cached = self._cache.get(key)
            return None if cached is None else _snapshot(cached, stale=True)
        self._store(key, balance)
        return _snapshot(balance, stale=False)

    def _expired(self, key: str) -> bool:
        return (self._now() - self._fetched_at.get(key, 0.0)) > self._ttl

    def _store(self, key: str, balance: CloudCreditBalance) -> None:
        self._cache[key] = balance
        self._fetched_at[key] = self._now()


def _snapshot(
    balance: CloudCreditBalance, *, stale: bool,
) -> CloudCreditSnapshot:
    return CloudCreditSnapshot(
        gift_cr=balance.gift_cr,
        purchased_cr=balance.purchased_cr,
        total_cr=balance.total_cr,
        stale=stale,
    )
