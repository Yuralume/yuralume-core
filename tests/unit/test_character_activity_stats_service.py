"""The hosted character-load gauge: total shelf vs. what still runs.

The service's whole job is to answer "active" the SAME way the due-job cluster
answers "will I reseed this chain": state stops (freeze / subscription lock)
come from SQL, the dormancy window (NF4) comes from each tier's control-plane
profile, and an unresolvable profile fails **open** because that is what the
cluster does with the same unresolvable profile.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.character_activity_stats import (
    CharacterActivityStatsService,
)
from kokoro_link.contracts.character_activity_stats import TierCharacterCounts
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)

_NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


class _FakeStats:
    def __init__(self, buckets, engaged=None):
        self._buckets = list(buckets)
        self._engaged = dict(engaged or {})
        self.engaged_calls: list[tuple[str, datetime]] = []

    async def counts_by_tier(self):
        return list(self._buckets)

    async def count_engaged_since(self, tier, cutoff):
        self.engaged_calls.append((tier, cutoff))
        return self._engaged.get(tier, 0)


class _FakeTierProfiles:
    def __init__(self, profiles, raises=()):
        self._profiles = dict(profiles)
        self._raises = set(raises)

    async def fetch(self, tier):
        if tier in self._raises:
            raise RuntimeError("control plane down")
        return self._profiles.get(tier)


def _profile(name: str, dormancy_days: int | None) -> AccountRuntimeProfile:
    return AccountRuntimeProfile(name=name, background_dormancy_days=dormancy_days)


@pytest.mark.asyncio
async def test_totals_every_bucket_and_counts_schedulable_when_no_tier_policy_exists():
    """Self-host / no runtime-config: no dormancy window anywhere, so "active"
    collapses to the state filter and no per-tier query is issued at all."""
    stats = _FakeStats([
        TierCharacterCounts(tier="standard", total=10, schedulable=7),
        TierCharacterCounts(tier="free", total=5, schedulable=5),
    ])
    service = CharacterActivityStatsService(stats=stats)

    counts = await service.collect(now=_NOW)

    assert (counts.total, counts.active) == (15, 12)
    assert stats.engaged_calls == []


@pytest.mark.asyncio
async def test_each_tier_gets_its_own_dormancy_cutoff():
    stats = _FakeStats(
        [
            TierCharacterCounts(tier="free", total=40, schedulable=30),
            TierCharacterCounts(tier="pro", total=10, schedulable=9),
        ],
        engaged={"free": 4, "pro": 6},
    )
    service = CharacterActivityStatsService(
        stats=stats,
        tier_profiles=_FakeTierProfiles({
            "free": _profile("free", 7),
            "pro": _profile("pro", 30),
        }),
    )

    counts = await service.collect(now=_NOW)

    assert counts.total == 50
    assert counts.active == 10
    assert stats.engaged_calls == [
        ("free", _NOW - timedelta(days=7)),
        ("pro", _NOW - timedelta(days=30)),
    ]


@pytest.mark.asyncio
async def test_tier_without_a_dormancy_knob_keeps_every_schedulable_character():
    """``background_dormancy_days = None`` is "never dormant", not "zero days"."""
    stats = _FakeStats(
        [TierCharacterCounts(tier="pro", total=8, schedulable=8)],
        engaged={"pro": 1},
    )
    service = CharacterActivityStatsService(
        stats=stats,
        tier_profiles=_FakeTierProfiles({"pro": _profile("pro", None)}),
    )

    counts = await service.collect(now=_NOW)

    assert counts.active == 8
    assert stats.engaged_calls == []


@pytest.mark.asyncio
async def test_unresolvable_tier_profile_fails_open_exactly_like_the_scheduler():
    """A control-plane outage must not make the card show a load drop the
    cluster is not having: the due-job resolver reads an unresolvable profile
    as "no dormancy", so this counts the same characters it would still run."""
    stats = _FakeStats(
        [
            TierCharacterCounts(tier="free", total=20, schedulable=20),
            TierCharacterCounts(tier="ghost", total=3, schedulable=3),
        ],
        engaged={"free": 2},
    )
    service = CharacterActivityStatsService(
        stats=stats,
        tier_profiles=_FakeTierProfiles(
            {"free": _profile("free", 7)}, raises={"free"},
        ),
    )

    counts = await service.collect(now=_NOW)

    # "free" raised → fail open (20); "ghost" has no control-plane profile at
    # all → also no dormancy policy (3).
    assert (counts.total, counts.active) == (23, 23)
    assert stats.engaged_calls == []


@pytest.mark.asyncio
async def test_a_fully_stopped_tier_costs_no_query_and_contributes_no_active():
    stats = _FakeStats(
        [TierCharacterCounts(tier="free", total=12, schedulable=0)],
        engaged={"free": 5},
    )
    service = CharacterActivityStatsService(
        stats=stats,
        tier_profiles=_FakeTierProfiles({"free": _profile("free", 7)}),
    )

    counts = await service.collect(now=_NOW)

    assert (counts.total, counts.active) == (12, 0)
    assert stats.engaged_calls == []


@pytest.mark.asyncio
async def test_active_can_never_exceed_the_schedulable_set_it_came_from():
    """Defensive clamp: an "active > total" card would be read as a bug in the
    platform rather than in the counter."""
    stats = _FakeStats(
        [TierCharacterCounts(tier="free", total=5, schedulable=3)],
        engaged={"free": 99},
    )
    service = CharacterActivityStatsService(
        stats=stats,
        tier_profiles=_FakeTierProfiles({"free": _profile("free", 7)}),
    )

    counts = await service.collect(now=_NOW)

    assert (counts.total, counts.active) == (5, 3)
