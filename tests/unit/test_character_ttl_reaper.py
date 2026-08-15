"""Per-tier character TTL sweep.

The TTL itself arrives from the cloud control-plane (``character_ttl_days``
on the tier's ``core_runtime_profile``), so these tests wire a stub tier
port rather than leaning on any tier name being special: Core has no
hardcoded tier->knob table since the demo site was retired (2026-08-06).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.account_runtime_profile import (
    AccountRuntimeProfileResolver,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.character_ttl_reaper import CharacterTtlReaper
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.value_objects.account_runtime_profile import (
    AccountRuntimeProfile,
)
from kokoro_link.infrastructure.repositories.in_memory_account_runtime_usage import (
    InMemoryAccountRuntimeUsageRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)


_NOW = datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc)

CAPPED_TIER = "capped"
"""A tier whose control-plane profile sets a 3-day ``character_ttl``."""

UNCAPPED_TIER = "plus"
"""A tier with no ``character_ttl`` — the reaper must never touch it."""


class _MutableClock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


class _StubTierPort:
    """Control-plane stand-in: only ``CAPPED_TIER`` carries a character TTL."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, tier: str) -> AccountRuntimeProfile | None:
        self.calls.append(tier)
        if tier == CAPPED_TIER:
            return AccountRuntimeProfile(
                name=CAPPED_TIER, character_ttl=timedelta(days=3),
            )
        return None


@pytest.mark.asyncio
async def test_reaper_deletes_character_past_its_tier_ttl() -> None:
    harness = await _build_harness(created_at=_NOW - timedelta(days=4))
    character_id = harness["character_id"]

    result = await harness["reaper"].run_once(now=_NOW)

    assert result.deleted_characters == 1
    assert result.expired_characters == 1
    assert await harness["character_repo"].get(character_id) is None


@pytest.mark.asyncio
async def test_ttl_alone_is_enough_to_reap_without_a_create_limit() -> None:
    """A tier may set ``character_ttl_days`` and nothing else.

    The reaper reads a character's birth date off the ``character_create``
    ledger row, and that row used to be written only when the tier also set
    ``daily_character_create_limit``. A TTL-only tier therefore produced no
    rows and a ceiling that silently never fired. The stub profile here sets
    the TTL alone, so this test fails if that coupling comes back.
    """
    harness = await _build_harness(created_at=_NOW - timedelta(days=4))
    assert (
        await harness["usage_repo"].count_events(
            operator_id="op-capped",
            event_type="character_create",
            since=_NOW - timedelta(days=30),
            until=_NOW,
        )
        == 1
    )

    result = await harness["reaper"].run_once(now=_NOW)

    assert result.deleted_characters == 1


@pytest.mark.asyncio
async def test_reaper_keeps_character_inside_its_tier_ttl() -> None:
    harness = await _build_harness(created_at=_NOW - timedelta(days=1))
    character_id = harness["character_id"]

    result = await harness["reaper"].run_once(now=_NOW)

    assert result.deleted_characters == 0
    assert await harness["character_repo"].get(character_id) is not None


@pytest.mark.asyncio
async def test_reaper_ignores_a_tier_without_a_character_ttl() -> None:
    harness = await _build_harness(
        created_at=_NOW - timedelta(days=4),
        cloud_tenant_tier=UNCAPPED_TIER,
    )
    character_id = harness["character_id"]

    result = await harness["reaper"].run_once(now=_NOW)

    assert result.deleted_characters == 0
    assert result.expired_characters == 0
    assert await harness["character_repo"].get(character_id) is not None


@pytest.mark.asyncio
async def test_tier_push_to_uncapped_stops_reaper_from_deleting_characters() -> None:
    """Regression: the reaper race a live tier push must close.

    An operator is on a TTL-capped tier and has a character aged past that
    TTL — the reaper WOULD delete it (see
    ``test_reaper_deletes_character_past_its_tier_ttl``). Cloud then pushes
    the tenant to an uncapped tier via
    ``set_cloud_tenant_tier_for_cloud_tenant`` *before the operator re-logs
    in*. The resolver now returns a profile without a ``character_ttl``, so
    the sweep must leave the (now paying) customer's character alone."""
    harness = await _build_harness(created_at=_NOW - timedelta(days=4))
    character_id = harness["character_id"]

    updated = await harness["operator_repo"].set_cloud_tenant_tier_for_cloud_tenant(
        "tenant_1", UNCAPPED_TIER,
    )
    assert updated == 1

    result = await harness["reaper"].run_once(now=_NOW)

    assert result.deleted_characters == 0
    assert await harness["character_repo"].get(character_id) is not None


class _LengthenAfterScanResolver:
    """Wraps a resolver and *lengthens* the TTL after the first resolve.

    The nastier half of the H4 race: the tier still sets a ``character_ttl``
    after the push, so a pre-delete check that only asks "is there still a
    TTL?" says yes and deletes a character that the new, longer TTL no longer
    considers expired.
    """

    def __init__(self, first: AccountRuntimeProfile, then: AccountRuntimeProfile) -> None:
        self._first = first
        self._then = then
        self.calls = 0

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        self.calls += 1
        return self._first if self.calls == 1 else self._then


@pytest.mark.asyncio
async def test_ttl_lengthened_between_scan_and_delete_spares_character() -> None:
    """A 4-day-old character expired under a 3-day TTL; the tier is pushed to
    a 30-day TTL before the delete. It must survive — the TTL is still set, so
    only re-evaluating *expiry* (not merely "is a TTL configured") catches it."""
    harness = await _build_harness(created_at=_NOW - timedelta(days=4))
    resolver = _LengthenAfterScanResolver(
        AccountRuntimeProfile(name=CAPPED_TIER, character_ttl=timedelta(days=3)),
        AccountRuntimeProfile(name="roomier", character_ttl=timedelta(days=30)),
    )
    reaper = CharacterTtlReaper(
        character_repository=harness["character_repo"],
        character_service=harness["character_service"],
        account_runtime_profile_resolver=resolver,
        account_runtime_usage_repository=harness["usage_repo"],
        clock=harness["clock"],
    )

    result = await reaper.run_once(now=_NOW)

    assert result.expired_characters == 1  # the scan did flag it
    assert result.deleted_characters == 0  # ...and the re-check saved it
    assert await harness["character_repo"].get(harness["character_id"]) is not None
    assert resolver.calls >= 2


class _FlipAfterScanResolver:
    """Wraps the real resolver and flips the tenant to an uncapped tier AFTER
    the first resolve of the target operator.

    Models the H4 race precisely: the scan-time TTL resolve still sees the
    capped profile (character expired), but the uncapped push lands *between*
    that scan and the per-character delete, so the pre-delete re-resolve sees
    no ``character_ttl`` and the sweep must leave the character alone.
    """

    def __init__(self, inner, operator_repo, *, tenant_id: str, operator_id: str) -> None:
        self._inner = inner
        self._operator_repo = operator_repo
        self._tenant_id = tenant_id
        self._operator_id = operator_id
        self.calls = 0

    async def resolve_for_operator(self, operator_id: str):
        self.calls += 1
        profile = await self._inner.resolve_for_operator(operator_id)
        if operator_id == self._operator_id and self.calls == 1:
            # Uncapped push lands right after the scan-time resolve returns.
            await self._operator_repo.set_cloud_tenant_tier_for_cloud_tenant(
                self._tenant_id, UNCAPPED_TIER,
            )
        return profile


@pytest.mark.asyncio
async def test_uncapped_push_between_scan_and_delete_spares_character() -> None:
    """H4: a character expired at scan time whose operator is moved to a tier
    without a character TTL BEFORE its per-character delete must not be
    reaped."""
    character_repo = InMemoryCharacterRepository()
    operator_repo = InMemoryOperatorProfileRepository()
    usage_repo = InMemoryAccountRuntimeUsageRepository()
    clock = _MutableClock(_NOW - timedelta(days=4))
    operator_id = "op-capped"
    await operator_repo.save(
        OperatorProfile(
            id=operator_id,
            display_name="Player",
            auth_provider="cloud",
            cloud_account_id="acct_1",
            cloud_tenant_id="tenant_1",
            cloud_tenant_tier=CAPPED_TIER,
        )
    )
    real_resolver = AccountRuntimeProfileResolver(
        operator_repo, tier_profile_port=_StubTierPort(),
    )
    character_service = CharacterService(
        character_repo,
        account_runtime_profile_resolver=real_resolver,
        account_runtime_usage_repository=usage_repo,
        clock=clock,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi"),
        user_id=operator_id,
    )
    clock.current = _NOW
    flipping_resolver = _FlipAfterScanResolver(
        real_resolver,
        operator_repo,
        tenant_id="tenant_1",
        operator_id=operator_id,
    )
    reaper = CharacterTtlReaper(
        character_repository=character_repo,
        character_service=character_service,
        account_runtime_profile_resolver=flipping_resolver,
        account_runtime_usage_repository=usage_repo,
        clock=clock,
    )

    result = await reaper.run_once(now=_NOW)

    assert result.deleted_characters == 0
    assert await character_repo.get(created.id) is not None
    # Proves the race window: the resolver was consulted at least twice for
    # the same operator (scan-time TTL check + pre-delete re-check).
    assert flipping_resolver.calls >= 2


async def _build_harness(
    *,
    created_at: datetime,
    cloud_tenant_tier: str = CAPPED_TIER,
) -> dict[str, object]:
    character_repo = InMemoryCharacterRepository()
    operator_repo = InMemoryOperatorProfileRepository()
    usage_repo = InMemoryAccountRuntimeUsageRepository()
    clock = _MutableClock(created_at)
    operator_id = "op-capped"
    await operator_repo.save(
        OperatorProfile(
            id=operator_id,
            display_name="Player",
            auth_provider="cloud",
            cloud_account_id="acct_1",
            cloud_tenant_id="tenant_1",
            cloud_tenant_tier=cloud_tenant_tier,
        )
    )
    profile_resolver = AccountRuntimeProfileResolver(
        operator_repo, tier_profile_port=_StubTierPort(),
    )
    character_service = CharacterService(
        character_repo,
        account_runtime_profile_resolver=profile_resolver,
        account_runtime_usage_repository=usage_repo,
        clock=clock,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi"),
        user_id=operator_id,
    )
    clock.current = _NOW
    reaper = CharacterTtlReaper(
        character_repository=character_repo,
        character_service=character_service,
        account_runtime_profile_resolver=profile_resolver,
        account_runtime_usage_repository=usage_repo,
        clock=clock,
    )
    return {
        "character_id": created.id,
        "character_repo": character_repo,
        "character_service": character_service,
        "operator_repo": operator_repo,
        "usage_repo": usage_repo,
        "clock": clock,
        "reaper": reaper,
    }
