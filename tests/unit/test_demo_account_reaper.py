from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.account_runtime_profile import (
    AccountRuntimeProfileResolver,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.demo_account_reaper import DemoAccountReaper
from kokoro_link.domain.entities.operator_profile import OperatorProfile
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


class _MutableClock:
    def __init__(self, now: datetime) -> None:
        self.current = now

    def now(self) -> datetime:
        return self.current


class _ReleaseHook:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, str]] = []

    async def release_demo_session(self, *, tenant_id: str, account_id: str) -> None:
        self.calls.append((tenant_id, account_id))
        if self.fail:
            raise RuntimeError("release failed")


@pytest.mark.asyncio
async def test_demo_account_reaper_deletes_expired_character_and_releases_session() -> None:
    harness = await _build_harness(created_at=_NOW - timedelta(days=4))
    character_id = harness["character_id"]

    result = await harness["reaper"].run_once(now=_NOW)

    assert result.deleted_characters == 1
    assert result.released_accounts == 1
    assert await harness["character_repo"].get(character_id) is None
    assert harness["release_hook"].calls == [("tenant_1", "acct_1")]


@pytest.mark.asyncio
async def test_demo_account_reaper_keeps_fresh_demo_character() -> None:
    harness = await _build_harness(created_at=_NOW - timedelta(days=1))
    character_id = harness["character_id"]

    result = await harness["reaper"].run_once(now=_NOW)

    assert result.deleted_characters == 0
    assert result.released_accounts == 0
    assert await harness["character_repo"].get(character_id) is not None
    assert harness["release_hook"].calls == []


@pytest.mark.asyncio
async def test_demo_account_reaper_ignores_non_demo_runtime_profile() -> None:
    harness = await _build_harness(
        created_at=_NOW - timedelta(days=4),
        cloud_tenant_tier="standard",
    )
    character_id = harness["character_id"]

    result = await harness["reaper"].run_once(now=_NOW)

    assert result.deleted_characters == 0
    assert await harness["character_repo"].get(character_id) is not None
    assert harness["release_hook"].calls == []


@pytest.mark.asyncio
async def test_demo_account_reaper_records_release_failure_after_delete() -> None:
    harness = await _build_harness(
        created_at=_NOW - timedelta(days=4),
        release_hook=_ReleaseHook(fail=True),
    )
    character_id = harness["character_id"]

    result = await harness["reaper"].run_once(now=_NOW)

    assert result.deleted_characters == 1
    assert result.release_failures == 1
    assert result.released_accounts == 0
    assert await harness["character_repo"].get(character_id) is None
    assert harness["release_hook"].calls == [("tenant_1", "acct_1")]


@pytest.mark.asyncio
async def test_tier_push_to_paid_stops_reaper_from_deleting_characters() -> None:
    """Regression: the reaper race a live tier push must close.

    An operator was demo and has a character aged past the 3-day demo TTL — the
    reaper WOULD delete it (see
    ``test_demo_account_reaper_deletes_expired_character_and_releases_session``).
    Cloud then pushes the tenant to a paid tier via
    ``set_cloud_tenant_tier_for_cloud_tenant`` *before the operator re-logs in*.
    The resolver now returns a profile without a ``character_ttl``, so the sweep
    must leave the (now paying) customer's character alone."""
    harness = await _build_harness(
        created_at=_NOW - timedelta(days=4), cloud_tenant_tier="demo",
    )
    character_id = harness["character_id"]

    updated = await harness["operator_repo"].set_cloud_tenant_tier_for_cloud_tenant(
        "tenant_1", "plus",
    )
    assert updated == 1

    result = await harness["reaper"].run_once(now=_NOW)

    assert result.deleted_characters == 0
    assert await harness["character_repo"].get(character_id) is not None
    assert harness["release_hook"].calls == []


class _FlipAfterScanResolver:
    """Wraps the real resolver and flips the tenant to paid AFTER the first
    resolve of the target operator.

    Models the H4 race precisely: the scan-time TTL resolve still sees the
    demo profile (character expired), but the paid push lands *between* that
    scan and the per-character delete, so the pre-delete re-resolve sees no
    ``character_ttl`` and the sweep must leave the character alone.
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
            # Paid push lands right after the scan-time resolve returns.
            await self._operator_repo.set_cloud_tenant_tier_for_cloud_tenant(
                self._tenant_id, "plus",
            )
        return profile


@pytest.mark.asyncio
async def test_paid_push_between_scan_and_delete_spares_character() -> None:
    """H4: a character expired at scan time whose operator is upgraded to a
    paid tier BEFORE its per-character delete must not be reaped."""
    character_repo = InMemoryCharacterRepository()
    operator_repo = InMemoryOperatorProfileRepository()
    usage_repo = InMemoryAccountRuntimeUsageRepository()
    clock = _MutableClock(_NOW - timedelta(days=4))
    release_hook = _ReleaseHook()
    operator_id = "op-demo"
    await operator_repo.save(
        OperatorProfile(
            id=operator_id,
            display_name="Demo Player",
            auth_provider="cloud",
            cloud_account_id="acct_1",
            cloud_tenant_id="tenant_1",
            cloud_tenant_tier="demo",
        )
    )
    real_resolver = AccountRuntimeProfileResolver(operator_repo)
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
    reaper = DemoAccountReaper(
        character_repository=character_repo,
        character_service=character_service,
        operator_profile_repository=operator_repo,
        account_runtime_profile_resolver=flipping_resolver,
        account_runtime_usage_repository=usage_repo,
        release_hook=release_hook,
        clock=clock,
    )

    result = await reaper.run_once(now=_NOW)

    assert result.deleted_characters == 0
    assert await character_repo.get(created.id) is not None
    assert release_hook.calls == []
    # Proves the race window: the resolver was consulted at least twice for
    # the same operator (scan-time TTL check + pre-delete re-check).
    assert flipping_resolver.calls >= 2


async def _build_harness(
    *,
    created_at: datetime,
    cloud_tenant_tier: str = "demo",
    release_hook: _ReleaseHook | None = None,
) -> dict[str, object]:
    character_repo = InMemoryCharacterRepository()
    operator_repo = InMemoryOperatorProfileRepository()
    usage_repo = InMemoryAccountRuntimeUsageRepository()
    clock = _MutableClock(created_at)
    release_hook = release_hook or _ReleaseHook()
    operator_id = "op-demo"
    await operator_repo.save(
        OperatorProfile(
            id=operator_id,
            display_name="Demo Player",
            auth_provider="cloud",
            cloud_account_id="acct_1",
            cloud_tenant_id="tenant_1",
            cloud_tenant_tier=cloud_tenant_tier,
        )
    )
    profile_resolver = AccountRuntimeProfileResolver(operator_repo)
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
    reaper = DemoAccountReaper(
        character_repository=character_repo,
        character_service=character_service,
        operator_profile_repository=operator_repo,
        account_runtime_profile_resolver=profile_resolver,
        account_runtime_usage_repository=usage_repo,
        release_hook=release_hook,
        clock=clock,
    )
    return {
        "character_id": created.id,
        "character_repo": character_repo,
        "operator_repo": operator_repo,
        "release_hook": release_hook,
        "reaper": reaper,
    }
