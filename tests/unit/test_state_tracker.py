"""Unit tests for StateChangeTracker and in-memory StateHistoryRepository."""

import pytest

from kokoro_link.application.services.state_tracker import StateChangeTracker
from kokoro_link.domain.entities.state_snapshot import (
    SOURCE_HEURISTIC,
    SOURCE_LLM_REFINEMENT,
    SOURCE_MANUAL,
    SOURCE_REST_RECOVERY,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_state_history import (
    InMemoryStateHistoryRepository,
)


def _state(
    emotion: str = "neutral",
    affection: int = 50,
    fatigue: int = 0,
    trust: int = 50,
    energy: int = 100,
) -> CharacterState:
    return CharacterState(
        emotion=emotion,
        affection=affection,
        fatigue=fatigue,
        trust=trust,
        energy=energy,
    )


@pytest.mark.asyncio
async def test_tracker_records_change() -> None:
    repo = InMemoryStateHistoryRepository()
    tracker = StateChangeTracker(repo)

    before = _state()
    after = _state(emotion="happy", affection=55)

    await tracker.record(
        character_id="char-1",
        source=SOURCE_HEURISTIC,
        before=before,
        after=after,
        trigger="使用者說了開心的話",
    )

    history = await repo.query("char-1")
    assert len(history) == 1
    assert history[0].source == SOURCE_HEURISTIC
    assert history[0].emotion == "happy"
    assert history[0].affection == 55
    assert history[0].trigger == "使用者說了開心的話"


@pytest.mark.asyncio
async def test_tracker_skips_no_op() -> None:
    repo = InMemoryStateHistoryRepository()
    tracker = StateChangeTracker(repo)

    state = _state()
    await tracker.record(
        character_id="char-1",
        source=SOURCE_HEURISTIC,
        before=state,
        after=state,
    )

    history = await repo.query("char-1")
    assert len(history) == 0


@pytest.mark.asyncio
async def test_multiple_sources_recorded() -> None:
    repo = InMemoryStateHistoryRepository()
    tracker = StateChangeTracker(repo)

    s0 = _state()
    s1 = _state(fatigue=10, energy=90)
    s2 = _state(fatigue=10, energy=90, emotion="warm")
    s3 = _state(fatigue=5, energy=95, emotion="warm")

    await tracker.record(character_id="c1", source=SOURCE_HEURISTIC, before=s0, after=s1)
    await tracker.record(character_id="c1", source=SOURCE_LLM_REFINEMENT, before=s1, after=s2)
    await tracker.record(character_id="c1", source=SOURCE_REST_RECOVERY, before=s2, after=s3)

    history = await repo.query("c1")
    assert len(history) == 3
    sources = [h.source for h in history]
    assert SOURCE_REST_RECOVERY in sources
    assert SOURCE_LLM_REFINEMENT in sources
    assert SOURCE_HEURISTIC in sources


@pytest.mark.asyncio
async def test_query_respects_limit() -> None:
    repo = InMemoryStateHistoryRepository()
    tracker = StateChangeTracker(repo)

    for i in range(10):
        await tracker.record(
            character_id="c1",
            source=SOURCE_HEURISTIC,
            before=_state(affection=i),
            after=_state(affection=i + 1),
        )

    history = await repo.query("c1", limit=3)
    assert len(history) == 3


@pytest.mark.asyncio
async def test_query_newest_first() -> None:
    repo = InMemoryStateHistoryRepository()
    tracker = StateChangeTracker(repo)

    await tracker.record(
        character_id="c1",
        source=SOURCE_HEURISTIC,
        before=_state(affection=50),
        after=_state(affection=51),
    )
    await tracker.record(
        character_id="c1",
        source=SOURCE_MANUAL,
        before=_state(affection=51),
        after=_state(affection=80),
    )

    history = await repo.query("c1")
    assert history[0].source == SOURCE_MANUAL  # newest
    assert history[1].source == SOURCE_HEURISTIC


@pytest.mark.asyncio
async def test_delete_for_character() -> None:
    repo = InMemoryStateHistoryRepository()
    tracker = StateChangeTracker(repo)

    await tracker.record(
        character_id="c1",
        source=SOURCE_HEURISTIC,
        before=_state(),
        after=_state(affection=55),
    )
    await tracker.record(
        character_id="c2",
        source=SOURCE_HEURISTIC,
        before=_state(),
        after=_state(affection=60),
    )

    count = await repo.delete_for_character("c1")
    assert count == 1

    assert len(await repo.query("c1")) == 0
    assert len(await repo.query("c2")) == 1


@pytest.mark.asyncio
async def test_isolation_between_characters() -> None:
    repo = InMemoryStateHistoryRepository()
    tracker = StateChangeTracker(repo)

    await tracker.record(
        character_id="c1", source=SOURCE_HEURISTIC,
        before=_state(), after=_state(affection=55),
    )
    await tracker.record(
        character_id="c2", source=SOURCE_MANUAL,
        before=_state(), after=_state(trust=80),
    )

    assert len(await repo.query("c1")) == 1
    assert len(await repo.query("c2")) == 1
    assert (await repo.query("c1"))[0].affection == 55
    assert (await repo.query("c2"))[0].trust == 80
