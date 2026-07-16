"""Idle-character auto-freeze reaper (CHARACTER_FREEZE_PLAN)."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.app_runtime_settings_service import (
    AppRuntimeSettingsService,
)
from kokoro_link.application.services.character_freeze_reaper import (
    CharacterFreezeReaper,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_runtime_settings import (
    InMemoryRuntimeSettingsRepository,
)

_NOW = datetime(2026, 7, 8, 12, 0, tzinfo=timezone.utc)


def _character(
    name: str,
    *,
    last_active_at: datetime | None,
    created_at: datetime | None = None,
    frozen: bool = False,
) -> Character:
    base = Character.create(
        name=name,
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=last_active_at,
        ),
    )
    return replace(
        base,
        created_at=created_at,
        frozen=frozen,
        frozen_at=_NOW if frozen else None,
    )


async def _settings(**values) -> AppRuntimeSettingsService:
    service = AppRuntimeSettingsService(InMemoryRuntimeSettingsRepository())
    if values:
        await service.set("character_freeze", values)
    return service


def _reaper(repo, settings) -> CharacterFreezeReaper:
    return CharacterFreezeReaper(
        character_repository=repo, settings_service=settings,
    )


@pytest.mark.asyncio
async def test_disabled_by_default_freezes_nothing() -> None:
    repo = InMemoryCharacterRepository()
    await repo.save(_character("Old", last_active_at=_NOW - timedelta(days=90)))
    reaper = _reaper(repo, await _settings())  # no config -> schema default (off)

    result = await reaper.run_once(now=_NOW)

    assert result.enabled is False
    assert result.frozen_characters == 0
    assert all(not c.frozen for c in await repo.list())


@pytest.mark.asyncio
async def test_freezes_idle_but_not_recently_active() -> None:
    repo = InMemoryCharacterRepository()
    idle = _character("Idle", last_active_at=_NOW - timedelta(days=40))
    fresh = _character("Fresh", last_active_at=_NOW - timedelta(days=2))
    await repo.save(idle)
    await repo.save(fresh)
    settings = await _settings(auto_freeze_enabled=True, idle_days_threshold=30)

    result = await _reaper(repo, settings).run_once(now=_NOW)

    assert result.enabled is True
    assert result.frozen_characters == 1
    assert (await repo.get(idle.id)).frozen is True
    assert (await repo.get(idle.id)).frozen_at == _NOW
    assert (await repo.get(fresh.id)).frozen is False


@pytest.mark.asyncio
async def test_idle_freeze_records_reason_idle() -> None:
    from kokoro_link.domain.entities.character import FREEZE_REASON_IDLE

    repo = InMemoryCharacterRepository()
    idle = _character("Idle", last_active_at=_NOW - timedelta(days=40))
    await repo.save(idle)
    settings = await _settings(auto_freeze_enabled=True, idle_days_threshold=30)

    await _reaper(repo, settings).run_once(now=_NOW)

    # Provenance must be tagged so a chat turn can still auto-thaw it, while
    # subscription / manual freezes stay distinguishable.
    stored = await repo.get(idle.id)
    assert stored.frozen is True
    assert stored.frozen_reason == FREEZE_REASON_IDLE


@pytest.mark.asyncio
async def test_never_chatted_uses_created_at_anchor() -> None:
    repo = InMemoryCharacterRepository()
    stale = _character(
        "NeverChatted", last_active_at=None,
        created_at=_NOW - timedelta(days=45),
    )
    brand_new = _character(
        "BrandNew", last_active_at=None,
        created_at=_NOW - timedelta(days=1),
    )
    no_anchor = _character("NoAnchor", last_active_at=None, created_at=None)
    for c in (stale, brand_new, no_anchor):
        await repo.save(c)
    settings = await _settings(auto_freeze_enabled=True, idle_days_threshold=30)

    await _reaper(repo, settings).run_once(now=_NOW)

    assert (await repo.get(stale.id)).frozen is True
    assert (await repo.get(brand_new.id)).frozen is False
    # No anchor at all -> cannot judge idleness -> left alone.
    assert (await repo.get(no_anchor.id)).frozen is False


@pytest.mark.asyncio
async def test_already_frozen_characters_are_not_rescanned() -> None:
    repo = InMemoryCharacterRepository()
    already = _character(
        "Already", last_active_at=_NOW - timedelta(days=99), frozen=True,
    )
    await repo.save(already)
    settings = await _settings(auto_freeze_enabled=True, idle_days_threshold=30)

    result = await _reaper(repo, settings).run_once(now=_NOW)

    # list_active() excludes it, so it is never a freeze candidate.
    assert result.scanned_characters == 0
    assert result.frozen_characters == 0
