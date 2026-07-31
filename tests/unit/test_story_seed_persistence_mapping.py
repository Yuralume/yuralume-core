"""StorySeed tier/regions persistence mapping — SA row + in-memory parity."""

import json

import pytest

from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.infrastructure.persistence.sa_story_repositories import (
    _apply_seed_updates,
    _row_to_seed,
    _seed_to_row,
)
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStorySeedRepository,
)


def _seed(**overrides) -> StorySeed:
    defaults = dict(
        seed_text="放學路上的手搖飲",
        tier="dramatic",
        regions=["tw", "jp"],
        external_id="map:a:001",
        pack_id="map_pack",
    )
    defaults.update(overrides)
    return StorySeed.create(**defaults)


def test_seed_to_row_serializes_tier_and_regions() -> None:
    row = _seed_to_row(_seed())
    assert row.tier == "dramatic"
    assert json.loads(row.regions) == ["tw", "jp"]


def test_row_to_seed_round_trips_tier_and_regions() -> None:
    original = _seed()
    restored = _row_to_seed(_seed_to_row(original))
    assert restored.tier == "dramatic"
    assert restored.regions == ("tw", "jp")


def test_apply_seed_updates_writes_tier_and_regions() -> None:
    row = _seed_to_row(_seed())
    _apply_seed_updates(row, _seed(tier="daily", regions=["global"]))
    assert row.tier == "daily"
    assert json.loads(row.regions) == ["global"]


def test_row_to_seed_defaults_when_columns_missing() -> None:
    """Pre-migration rows / partial schemas fall back to the historical
    behaviour: daily tier, drawable everywhere."""

    class _LegacyRow:
        id = "legacy-1"
        seed_text = "舊種子"
        tags = "[]"
        world_frames = '["any"]'
        weight = 1.0
        cooldown_days = 7
        enabled = True
        language = "zh-TW"
        character_id = None
        external_id = None
        pack_id = None
        created_at = _seed_to_row(_seed()).created_at
        updated_at = _seed_to_row(_seed()).updated_at

    restored = _row_to_seed(_LegacyRow())
    assert restored.tier == "daily"
    assert restored.regions == ("global",)


def test_row_to_seed_tolerates_malformed_regions_json() -> None:
    row = _seed_to_row(_seed())
    row.regions = "not-json"
    restored = _row_to_seed(row)
    assert restored.regions == ("global",)


@pytest.mark.asyncio
async def test_in_memory_upsert_carries_tier_and_regions() -> None:
    repo = InMemoryStorySeedRepository()
    await repo.upsert_by_external_id(_seed(tier="daily", regions=["global"]))

    updated = await repo.upsert_by_external_id(
        _seed(tier="dramatic", regions=["jp"]),
    )

    assert updated.tier == "dramatic"
    assert updated.regions == ("jp",)
    stored = await repo.list_by_pack("map_pack")
    assert len(stored) == 1
    assert stored[0].tier == "dramatic"
    assert stored[0].regions == ("jp",)
