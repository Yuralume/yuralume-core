"""Fusion material-richness stats service (Creator Studio C1-P1).

Covers: selection parity with the shared brief slice, threshold grading
(rich / ok / sparse), operator config overrides, low-salience exclusion,
and fail-soft on a memory-store error.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.app_runtime_settings_service import (
    AppRuntimeSettingsService,
)
from kokoro_link.application.services.fusion_character_brief import (
    select_brief_memories,
)
from kokoro_link.application.services.fusion_material_stats import (
    FusionMaterialStatsService,
)
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_runtime_settings import (
    InMemoryRuntimeSettingsRepository,
)


def _mem(character_id: str, *, length: int, salience: float) -> MemoryItem:
    return MemoryItem.create(
        character_id=character_id,
        kind=MemoryKind.EPISODIC,
        content="x" * length,
        salience=salience,
    )


async def _seed(repo: InMemoryMemoryRepository, character_id: str, items):
    for item in items:
        await repo.add(item)


def _service(
    repo: MemoryRepositoryPort | None,
    settings: AppRuntimeSettingsService,
) -> FusionMaterialStatsService:
    return FusionMaterialStatsService(
        memory_repository=repo, settings_service=settings,
    )


def _settings() -> AppRuntimeSettingsService:
    return AppRuntimeSettingsService(InMemoryRuntimeSettingsRepository())


@pytest.mark.asyncio
async def test_default_thresholds_grade_rich_ok_sparse() -> None:
    repo = InMemoryMemoryRepository()
    # rich: 8 memories × 150 chars = 1200 chars (>=8 count, >=1000 chars)
    await _seed(repo, "rich", [_mem("rich", length=150, salience=0.9)] * 8)
    # ok: 5 memories × 100 chars = 500 chars (>=3 count, >=300 chars)
    await _seed(repo, "ok", [_mem("ok", length=100, salience=0.8)] * 5)
    # sparse: 2 memories × 50 chars = 100 chars (below ok floor)
    await _seed(repo, "sparse", [_mem("sparse", length=50, salience=0.7)] * 2)

    service = _service(repo, _settings())
    stats = await service.stats_for(["rich", "ok", "sparse", "empty"])

    by_id = {s.character_id: s for s in stats}
    assert by_id["rich"].tier == "rich"
    assert by_id["rich"].memory_count == 8
    assert by_id["rich"].total_chars == 1200
    assert by_id["ok"].tier == "ok"
    assert by_id["ok"].memory_count == 5
    assert by_id["ok"].total_chars == 500
    assert by_id["sparse"].tier == "sparse"
    # A character with no memories at all is sparse / 0, never absent.
    assert by_id["empty"].tier == "sparse"
    assert by_id["empty"].memory_count == 0
    assert by_id["empty"].total_chars == 0


@pytest.mark.asyncio
async def test_stats_match_shared_brief_selection() -> None:
    """Counts must equal the exact slice the fusion brief would pull."""
    repo = InMemoryMemoryRepository()
    await _seed(repo, "c", [_mem("c", length=120, salience=0.9)] * 6)

    stats = await _service(repo, _settings()).stats_for(["c"])
    chosen = await select_brief_memories(repo, "c")

    assert stats[0].memory_count == len(chosen)
    assert stats[0].total_chars == sum(len(m.content) for m in chosen)


@pytest.mark.asyncio
async def test_low_salience_memories_excluded_like_brief() -> None:
    """Memories below the brief's salience floor never count toward tier."""
    repo = InMemoryMemoryRepository()
    # 5 above-floor + 3 below-floor (0.2 < 0.3 min_salience).
    await _seed(repo, "c", [_mem("c", length=100, salience=0.9)] * 5)
    await _seed(repo, "c", [_mem("c", length=100, salience=0.2)] * 3)

    stats = await _service(repo, _settings()).stats_for(["c"])

    # Only the 5 above-floor memories are visible to the brief.
    assert stats[0].memory_count == 5
    assert stats[0].total_chars == 500
    assert stats[0].tier == "ok"


@pytest.mark.asyncio
async def test_operator_config_override_raises_bar() -> None:
    """Tightened thresholds re-grade the same material downward."""
    repo = InMemoryMemoryRepository()
    await _seed(repo, "c", [_mem("c", length=100, salience=0.8)] * 5)

    settings = _settings()
    # Demand 6 memories / 800 chars for ``ok``; 5×100 now falls short.
    await settings.set(
        "fusion_material",
        {
            "ok_min_count": 6,
            "ok_min_chars": 800,
            "rich_min_count": 12,
            "rich_min_chars": 2000,
        },
    )

    stats = await _service(repo, settings).stats_for(["c"])
    assert stats[0].memory_count == 5
    assert stats[0].tier == "sparse"


@pytest.mark.asyncio
async def test_operator_config_override_lowers_bar_to_rich() -> None:
    repo = InMemoryMemoryRepository()
    await _seed(repo, "c", [_mem("c", length=100, salience=0.8)] * 5)

    settings = _settings()
    await settings.set(
        "fusion_material",
        {
            "ok_min_count": 1,
            "ok_min_chars": 1,
            "rich_min_count": 2,
            "rich_min_chars": 100,
        },
    )

    stats = await _service(repo, settings).stats_for(["c"])
    assert stats[0].tier == "rich"


class _RaisingMemoryRepo(InMemoryMemoryRepository):
    async def query(self, *args, **kwargs):  # type: ignore[override]
        raise RuntimeError("memory store unavailable")


@pytest.mark.asyncio
async def test_memory_query_failure_is_fail_soft() -> None:
    """A query error degrades to sparse / 0, never propagates."""
    service = _service(_RaisingMemoryRepo(), _settings())
    stats = await service.stats_for(["c"])
    assert stats[0].tier == "sparse"
    assert stats[0].memory_count == 0
    assert stats[0].total_chars == 0


@pytest.mark.asyncio
async def test_empty_input_returns_empty() -> None:
    service = _service(InMemoryMemoryRepository(), _settings())
    assert await service.stats_for([]) == []
