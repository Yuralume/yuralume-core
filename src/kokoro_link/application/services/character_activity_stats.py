"""How many characters exist, and how many still cost background work.

The Cloud admin console's dashboard reads this through the internal channel to
watch cluster load. "Active" is defined as *what the reconciler would actually
reseed*, so the card and the cluster cannot disagree:

* not frozen and not subscription-locked — the state half, counted in SQL
  (``CharacterActivityStatsPort.counts_by_tier``), identical to the filter
  ``list_active`` applies before a reconcile pass;
* not dormant (NF4) — the *policy* half, which no column can answer: the
  window is ``background_dormancy_days`` on the tier's control-plane runtime
  profile, so each tier present in the table gets its own cutoff.

**Fail-open, on purpose, in exactly the same direction the scheduler fails.**
A tier whose profile cannot be resolved (control plane down, no runtime-config,
self-host) yields no dormancy window, so its schedulable characters all count
as active — because that is precisely what the due-job cluster does with the
same unresolvable profile (see ``resolve_profile_or_none``). A stat that
guessed the other way would show a load drop at the exact moment the cluster
kept the load.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from kokoro_link.contracts.character_activity_stats import (
    CharacterActivityStatsPort,
)
from kokoro_link.contracts.clock import ClockPort
from kokoro_link.contracts.cloud_tier_runtime_profile import (
    TierRuntimeProfilePort,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CharacterActivityStats:
    """The whole shelf, and the part of it that is still running."""

    total: int
    active: int


class CharacterActivityStatsService:
    __slots__ = ("_stats", "_tier_profiles", "_clock")

    def __init__(
        self,
        *,
        stats: CharacterActivityStatsPort,
        tier_profiles: TierRuntimeProfilePort | None = None,
        clock: ClockPort | None = None,
    ) -> None:
        self._stats = stats
        self._tier_profiles = tier_profiles
        self._clock = clock

    async def collect(self, *, now: datetime | None = None) -> CharacterActivityStats:
        resolved = now or self._now()
        buckets = await self._stats.counts_by_tier()
        total = 0
        active = 0
        for bucket in buckets:
            total += bucket.total
            if bucket.schedulable <= 0:
                continue
            dormancy_days = await self._dormancy_days(bucket.tier)
            if dormancy_days is None:
                active += bucket.schedulable
                continue
            cutoff = resolved - timedelta(days=dormancy_days)
            engaged = await self._stats.count_engaged_since(bucket.tier, cutoff)
            # The engaged query carries the same schedulable filter, so it can
            # never exceed the bucket; clamping anyway keeps a future divergence
            # from printing an "active > total" card.
            active += min(engaged, bucket.schedulable)
        return CharacterActivityStats(total=total, active=active)

    async def _dormancy_days(self, tier: str) -> int | None:
        """The tier's NF4 window, or ``None`` for "no dormancy policy known"."""
        if self._tier_profiles is None:
            return None
        try:
            profile = await self._tier_profiles.fetch(tier)
        except Exception:
            # The cached resolver already absorbs transport failures; this is
            # the belt for anything it does not. Same fail-open answer.
            _LOGGER.warning(
                "character activity stats: tier profile unresolved tier=%s", tier,
            )
            return None
        if profile is None:
            return None
        return profile.background_dormancy_days

    def _now(self) -> datetime:
        if self._clock is not None:
            return self._clock.now()
        return datetime.now(timezone.utc)
