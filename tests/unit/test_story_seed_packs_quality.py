"""Bundled story-seed pack quality gate (STORY_SEED_ENRICHMENT_PLAN §1/§4).

Loads every pack under ``data/story_seeds/`` and enforces the structural
invariants the two-month no-repeat design depends on:

* ``external_id`` unique across all packs (idempotent upsert key);
* every seed parses cleanly (tier whitelist, regions vocabulary);
* every ``daily`` seed has ``cooldown_days >= 60``;
* pool depth: each frame x region daily context holds >= 70 seeds.

This is a data gate, not a code gate — it is EXPECTED to fail until the
content batches raise the legacy packs' cooldowns and pool sizes (that
work runs in parallel with SE0; see the plan's SE2 slice).
"""

from collections import Counter

import pytest
import yaml

from kokoro_link.application.services.story_seed_importer import (
    StorySeedImporter,
    default_pack_paths,
)
from kokoro_link.domain.entities.story_seed import (
    KNOWN_SEED_REGIONS,
    SEED_TIER_DAILY,
    SEED_TIERS,
)
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStorySeedRepository,
)

MIN_DAILY_COOLDOWN_DAYS = 60
MIN_DAILY_POOL_PER_CONTEXT = 70


def _load_raw_seed_entries() -> list[dict]:
    entries: list[dict] = []
    for path in default_pack_paths():
        with path.open("r", encoding="utf-8") as f:
            pack = yaml.safe_load(f)
        for raw in pack.get("seeds", []) or []:
            if isinstance(raw, dict):
                entries.append(raw)
    return entries


def test_bundled_packs_exist() -> None:
    assert default_pack_paths(), "no bundled packs under data/story_seeds/"


def test_external_ids_globally_unique() -> None:
    counts = Counter(
        (raw.get("external_id") or "").strip()
        for raw in _load_raw_seed_entries()
    )
    duplicates = sorted(eid for eid, n in counts.items() if eid and n > 1)
    assert not duplicates, f"duplicate external_ids across packs: {duplicates}"


@pytest.mark.asyncio
async def test_all_bundled_seeds_parse_cleanly() -> None:
    """Every shipped seed must pass the importer's validation — tier
    whitelist and regions vocabulary included."""
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo)

    report = await importer.import_paths(default_pack_paths())

    assert report.errors == (), f"pack validation errors: {report.errors}"
    assert report.skipped == 0


@pytest.mark.asyncio
async def test_tier_and_regions_values_are_legal() -> None:
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo)
    await importer.import_paths(default_pack_paths())

    seeds = await repo.list_for_character(
        "quality-gate", include_global=True, enabled_only=False,
    )
    assert seeds
    for seed in seeds:
        assert seed.tier in SEED_TIERS, (
            f"{seed.external_id}: illegal tier {seed.tier!r}"
        )
        illegal = [r for r in seed.regions if r not in KNOWN_SEED_REGIONS]
        assert not illegal, (
            f"{seed.external_id}: illegal regions {illegal!r}"
        )


@pytest.mark.asyncio
async def test_daily_seeds_cooldown_supports_two_month_rotation() -> None:
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo)
    await importer.import_paths(default_pack_paths())

    seeds = await repo.list_for_character(
        "quality-gate", include_global=True, enabled_only=False,
    )
    offenders = sorted(
        f"{seed.external_id} (cooldown {seed.cooldown_days})"
        for seed in seeds
        if seed.tier == SEED_TIER_DAILY
        and seed.cooldown_days < MIN_DAILY_COOLDOWN_DAYS
    )
    assert not offenders, (
        f"{len(offenders)} daily seeds below "
        f"{MIN_DAILY_COOLDOWN_DAYS}-day cooldown: {offenders[:10]}..."
    )


@pytest.mark.asyncio
async def test_daily_pool_depth_per_frame_region_context() -> None:
    """Each frame x region daily context needs >= 70 drawable seeds so a
    60-day rotation never hits an empty day. A seed counts toward a
    context when its world_frames fit the frame and its regions fit the
    region (global seeds count everywhere)."""
    repo = InMemoryStorySeedRepository()
    importer = StorySeedImporter(repo)
    await importer.import_paths(default_pack_paths())

    seeds = await repo.list_for_character(
        "quality-gate", include_global=True, enabled_only=False,
    )
    daily = [s for s in seeds if s.tier == SEED_TIER_DAILY and s.enabled]

    frames = sorted(
        {f for s in daily for f in s.world_frames if f != "any"},
    ) or ["modern"]
    regions: list[str | None] = [None, "tw", "jp", "west"]

    shortfalls: list[str] = []
    for frame in frames:
        for region in regions:
            pool = [
                s for s in daily
                if s.fits_frame(frame) and s.fits_region(region)
            ]
            if len(pool) < MIN_DAILY_POOL_PER_CONTEXT:
                shortfalls.append(
                    f"frame={frame} region={region}: {len(pool)}"
                    f" < {MIN_DAILY_POOL_PER_CONTEXT}",
                )
    assert not shortfalls, f"thin daily pools: {shortfalls}"
