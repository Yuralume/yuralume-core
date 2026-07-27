"""Core-side view of the hosted credit ("螢火") balance read endpoint.

In cloud mode a player's balance lives in the Cloud User service ledger, which
is the single source of truth for both pools and the full lifecycle ledger. Core
never mirrors that state — it proxies a display-only snapshot so the in-game
badge can answer the one question the player has mid-play: *how much is left?*
Every detail view (per-charge ledger, top-up) stays in the Portal.

See ``Yuralume-Cloud/docs/plans/HOSTED_PLAYER_UX_PLAN.md`` §4.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class CreditBalanceUnavailable(RuntimeError):
    """Raised when the hosted credit balance cannot be read.

    Covers transport failures, timeouts and any non-2xx upstream answer. An
    *unknown* tenant is NOT an error — the User service answers 200 with an
    all-zero balance — so this exception always means "we do not know", never
    "the player has nothing".
    """


@dataclass(frozen=True, slots=True)
class CloudCreditBalance:
    """Immutable balance snapshot as reported by the User service."""

    gift_cr: float = 0.0
    purchased_cr: float = 0.0
    total_cr: float = 0.0
    next_gift_expiry: str | None = None
    """ISO-8601 instant at which the current gift pool lapses, if any."""


class CreditBalancePort(Protocol):
    async def fetch(self, tenant_id: str) -> CloudCreditBalance:
        """Read the current balance for ``tenant_id``.

        Raises :class:`CreditBalanceUnavailable` on any failure; the caching
        service absorbs that and serves last-known-good instead."""
