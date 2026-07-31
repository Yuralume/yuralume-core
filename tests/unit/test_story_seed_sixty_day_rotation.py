"""Two-month no-repeat invariant — 60-day gacha simulation.

SE0 acceptance (STORY_SEED_ENRICHMENT_PLAN §1): with a daily pool of
>= 70 seeds whose ``cooldown_days >= 60``, sixty consecutive daily rolls
must produce zero repeats and zero empty days, and dramatic seeds must
never surface. This file simulates the loop with a synthetic pool and a
fixed rng; the bundled-pack quality gate lives in
``test_story_seed_packs_quality.py``.
"""

import random
from datetime import date as date_type, timedelta

import pytest

from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
    InMemoryStorySeedRepository,
)

SIMULATED_DAYS = 60
DAILY_POOL_SIZE = 75  # >= 70 per the plan (60 days + buffer)


def _character() -> Character:
    return Character(
        id="sim-c1", name="Sim", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        world_frame="modern",
    )


def _daily_pool() -> list[StorySeed]:
    return [
        StorySeed.create(
            seed_text=f"日常種子 {i:03d}",
            world_frames=["any"],
            cooldown_days=60,
            weight=1.0 + (i % 5) * 0.2,  # mixed weights, all positive
        )
        for i in range(DAILY_POOL_SIZE)
    ]


def _dramatic_pool() -> list[StorySeed]:
    return [
        StorySeed.create(
            seed_text=f"劇情種子 {i:03d}",
            world_frames=["any"],
            tier="dramatic",
            cooldown_days=30,
        )
        for i in range(10)
    ]


@pytest.mark.asyncio
async def test_sixty_days_zero_repeats_zero_empty_days() -> None:
    seeds_repo = InMemoryStorySeedRepository()
    events_repo = InMemoryStoryEventRepository()
    dramatic_ids = set()
    for seed in _daily_pool():
        await seeds_repo.add(seed)
    for seed in _dramatic_pool():
        dramatic_ids.add(seed.id)
        await seeds_repo.add(seed)

    gacha = StoryGachaService(
        seed_repository=seeds_repo,
        event_repository=events_repo,
        rng=random.Random(20260729),
    )
    character = _character()
    start = date_type(2026, 8, 1)

    picked_ids: list[str] = []
    for day_offset in range(SIMULATED_DAYS):
        today = start + timedelta(days=day_offset)
        result = await gacha.roll(character=character, today=today, count=1)
        # Zero empty days.
        assert result.picked, f"empty pool on day {day_offset} ({today})"
        seed = result.picked[0]
        picked_ids.append(seed.id)
        # Persist the roll so cooldown accounting sees it, exactly as
        # StoryEventService does after expansion.
        await events_repo.add(
            StoryEvent.create(
                character_id=character.id,
                date=today.isoformat(),
                seed_id=seed.id,
                narrative="模擬敘事",
            ),
        )

    # Zero repeats across the whole window.
    assert len(set(picked_ids)) == SIMULATED_DAYS
    # Dramatic seeds never surface in the daily rotation.
    assert not (set(picked_ids) & dramatic_ids)


@pytest.mark.asyncio
async def test_sixty_days_regional_context_stays_full() -> None:
    """A regional player draws from global + own-region seeds; the mixed
    pool for one frame x region context must also sustain 60 days."""
    seeds_repo = InMemoryStorySeedRepository()
    events_repo = InMemoryStoryEventRepository()
    # 40 global + 35 tw = 75 eligible for region="tw"; jp seeds must not
    # leak in and must not be needed to keep the pool full.
    for i in range(40):
        await seeds_repo.add(
            StorySeed.create(
                seed_text=f"全球日常 {i:03d}", world_frames=["any"],
                cooldown_days=60,
            ),
        )
    tw_ids = set()
    for i in range(35):
        seed = StorySeed.create(
            seed_text=f"台味日常 {i:03d}", world_frames=["any"],
            regions=["tw"], cooldown_days=60,
        )
        tw_ids.add(seed.id)
        await seeds_repo.add(seed)
    jp_ids = set()
    for i in range(10):
        seed = StorySeed.create(
            seed_text=f"日味日常 {i:03d}", world_frames=["any"],
            regions=["jp"], cooldown_days=60,
        )
        jp_ids.add(seed.id)
        await seeds_repo.add(seed)

    gacha = StoryGachaService(
        seed_repository=seeds_repo,
        event_repository=events_repo,
        rng=random.Random(42),
    )
    character = _character()
    start = date_type(2026, 8, 1)

    picked_ids: list[str] = []
    for day_offset in range(SIMULATED_DAYS):
        today = start + timedelta(days=day_offset)
        result = await gacha.roll(
            character=character, today=today, count=1, region="tw",
        )
        assert result.picked, f"empty pool on day {day_offset} ({today})"
        seed = result.picked[0]
        picked_ids.append(seed.id)
        await events_repo.add(
            StoryEvent.create(
                character_id=character.id,
                date=today.isoformat(),
                seed_id=seed.id,
                narrative="模擬敘事",
            ),
        )

    assert len(set(picked_ids)) == SIMULATED_DAYS
    assert not (set(picked_ids) & jp_ids)
