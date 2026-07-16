"""StoryGachaService — seed picking logic."""

import random
from datetime import date as date_type, datetime, timezone

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


def _default_state() -> CharacterState:
    return CharacterState(
        emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
    )


def _character(frame: str = "modern") -> Character:
    return Character.create(
        name="Test", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[], state=_default_state(),
        world_frame=frame,
    )


async def _seed(repo: InMemoryStorySeedRepository, seeds: list[StorySeed]) -> None:
    for s in seeds:
        await repo.add(s)


@pytest.mark.asyncio
async def test_rolls_nothing_when_pool_is_empty() -> None:
    seeds = InMemoryStorySeedRepository()
    events = InMemoryStoryEventRepository()
    gacha = StoryGachaService(
        seed_repository=seeds, event_repository=events,
        rng=random.Random(0),
    )

    result = await gacha.roll(
        character=_character(), today=date_type(2026, 4, 20), count=1,
    )
    assert result.picked == ()
    assert result.eligible_count == 0


@pytest.mark.asyncio
async def test_rolls_eligible_seed_when_pool_has_matching_frame() -> None:
    seeds = InMemoryStorySeedRepository()
    events = InMemoryStoryEventRepository()
    await _seed(seeds, [
        StorySeed.create(seed_text="modern thing", world_frames=["modern"]),
    ])
    gacha = StoryGachaService(
        seed_repository=seeds, event_repository=events,
        rng=random.Random(0),
    )

    result = await gacha.roll(
        character=_character(), today=date_type(2026, 4, 20), count=1,
    )
    assert len(result.picked) == 1
    assert result.picked[0].seed_text == "modern thing"


@pytest.mark.asyncio
async def test_frame_mismatch_excludes_seed() -> None:
    seeds = InMemoryStorySeedRepository()
    events = InMemoryStoryEventRepository()
    await _seed(seeds, [
        StorySeed.create(seed_text="fantasy only", world_frames=["fantasy"]),
    ])
    gacha = StoryGachaService(
        seed_repository=seeds, event_repository=events,
        rng=random.Random(0),
    )

    result = await gacha.roll(
        character=_character(frame="modern"), today=date_type(2026, 4, 20),
        count=1,
    )
    assert result.picked == ()


@pytest.mark.asyncio
async def test_cooldown_blocks_recent_seed() -> None:
    seeds = InMemoryStorySeedRepository()
    events = InMemoryStoryEventRepository()
    seed = StorySeed.create(
        seed_text="s", world_frames=["modern"], cooldown_days=14,
    )
    await _seed(seeds, [seed])
    await events.add(
        StoryEvent.create(
            character_id="c1", date="2026-04-15", seed_id=seed.id,
            narrative="previous",
        ),
    )
    # Rebuild with the same character id used above.
    character = _character()
    # Override id so last_roll_dates matches.
    character = Character(
        id="c1", name=character.name, summary=character.summary,
        personality=character.personality, interests=character.interests,
        speaking_style=character.speaking_style,
        boundaries=character.boundaries, state=character.state,
        world_frame="modern",
    )
    gacha = StoryGachaService(
        seed_repository=seeds, event_repository=events,
        rng=random.Random(0),
    )

    # Only 5 days later — inside 14-day cooldown → nothing eligible.
    result = await gacha.roll(
        character=character, today=date_type(2026, 4, 20), count=1,
    )
    assert result.picked == ()
    assert result.reason_if_empty and "cooldown" in result.reason_if_empty


@pytest.mark.asyncio
async def test_cooldown_expires_and_seed_becomes_eligible_again() -> None:
    seeds = InMemoryStorySeedRepository()
    events = InMemoryStoryEventRepository()
    seed = StorySeed.create(
        seed_text="s", world_frames=["modern"], cooldown_days=3,
    )
    await _seed(seeds, [seed])
    character = Character(
        id="c1", name="T", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[], state=_default_state(),
        world_frame="modern",
    )
    await events.add(
        StoryEvent.create(
            character_id="c1", date="2026-04-10", seed_id=seed.id,
            narrative="old",
        ),
    )
    gacha = StoryGachaService(
        seed_repository=seeds, event_repository=events,
        rng=random.Random(0),
    )

    # 10 days later — well past 3-day cooldown.
    result = await gacha.roll(
        character=character, today=date_type(2026, 4, 20), count=1,
    )
    assert len(result.picked) == 1


@pytest.mark.asyncio
async def test_character_specific_seed_preferred_over_global() -> None:
    seeds = InMemoryStorySeedRepository()
    events = InMemoryStoryEventRepository()
    global_seed = StorySeed.create(seed_text="global", world_frames=["modern"])
    char_seed = StorySeed.create(
        seed_text="private", world_frames=["modern"], character_id="c1",
    )
    await _seed(seeds, [global_seed, char_seed])

    character = Character(
        id="c1", name="T", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[], state=_default_state(),
        world_frame="modern",
    )
    gacha = StoryGachaService(
        seed_repository=seeds, event_repository=events,
        rng=random.Random(0),
    )

    # With 2 eligible seeds and count=2, both come back.
    result = await gacha.roll(
        character=character, today=date_type(2026, 4, 20), count=2,
    )
    assert {s.seed_text for s in result.picked} == {"global", "private"}
