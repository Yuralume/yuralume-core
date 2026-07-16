"""Core-side view of the control-plane per-tier runtime profile (plan H2).

In cloud mode Core asks the control-plane for the ``AccountRuntimeProfile`` that
governs a paid tier (character/message/media limits, proactive cadence, feature
flags) instead of hardcoding a tier->knob mapping. The profile is cached and
served from a warm key on the hot path; a control-plane outage serves the last
known good value (or ``None``) rather than failing the operator's request.
"""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)


class TierRuntimeProfileUnavailable(RuntimeError):
    """Raised when the control-plane tier runtime profile cannot be fetched."""


class TierRuntimeProfilePort(Protocol):
    async def fetch(self, tier: str) -> AccountRuntimeProfile | None:
        """Return the control-plane runtime profile for a paid ``tier``.

        ``None`` means the tier has no control-plane profile (the transport
        answered a clean 404) and the caller should fall back to its default
        policy. Implementations raise ``TierRuntimeProfileUnavailable`` on
        transport / server errors; the cached resolver absorbs that so
        ``resolve`` never raises."""
