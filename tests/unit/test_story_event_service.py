"""StoryEventService orchestration — roll → expand → persist → memorialize."""

import random
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.story_event_service import StoryEventService
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.contracts.story import StoryEventExpanderPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
    InMemoryStorySeedRepository,
)


class _ScriptedExpander(StoryEventExpanderPort):
    def __init__(self, narrative: str = "擴寫文字", tone: str | None = "peaceful") -> None:
        self._narrative = narrative
        self._tone = tone
        self.call_count = 0

    async def expand(self, **_kwargs):  # noqa: ANN003
        self.call_count += 1
        return (self._narrative, self._tone)


class _OperatorProfileService:
    async def get_for_user(self, user_id: str) -> OperatorProfile:
        return OperatorProfile(
            id=user_id,
            display_name=user_id,
            timezone_id="Asia/Taipei",
        )


def _character() -> Character:
    return Character.create(
        name="Yuki", summary="", personality=[], interests=[],
        speaking_style="natural", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        world_frame="modern",
    )


async def _wire(
    *,
    seed_texts: list[str],
    expander: StoryEventExpanderPort,
    operator_profile_service=None,
) -> tuple[
    StoryEventService,
    InMemoryStorySeedRepository,
    InMemoryStoryEventRepository,
    InMemoryMemoryRepository,
]:
    seeds_repo = InMemoryStorySeedRepository()
    events_repo = InMemoryStoryEventRepository()
    memory_repo = InMemoryMemoryRepository()
    for text in seed_texts:
        await seeds_repo.add(
            StorySeed.create(seed_text=text, world_frames=["modern"]),
        )
    gacha = StoryGachaService(
        seed_repository=seeds_repo, event_repository=events_repo,
        rng=random.Random(0),
    )
    service = StoryEventService(
        gacha=gacha,
        expander=expander,
        event_repository=events_repo,
        memory_repository=memory_repo,
        embedder=None,
        local_tz=timezone.utc,
        operator_profile_service=operator_profile_service,
    )
    return service, seeds_repo, events_repo, memory_repo


@pytest.mark.asyncio
async def test_ensure_today_rolls_writes_event_and_memory() -> None:
    expander = _ScriptedExpander("今天在咖啡店看到一隻可愛的柴犬。", "happy")
    service, _, events_repo, memory_repo = await _wire(
        seed_texts=["咖啡店看到可愛的狗"], expander=expander,
    )

    character = _character()
    report = await service.ensure_today(
        character, now=datetime(2026, 4, 20, 10, tzinfo=timezone.utc),
    )

    assert report.newly_rolled == 1
    assert len(report.events) == 1
    assert report.events[0].narrative.startswith("今天在咖啡店")
    # Memorialized → flag set + episodic memory written.
    stored = await events_repo.get_for_day(character.id, "2026-04-20")
    assert stored[0].memorialized is True
    memories = await memory_repo.query(character.id, limit=5)
    assert any(m.kind == MemoryKind.EPISODIC for m in memories)


@pytest.mark.asyncio
async def test_ensure_today_is_idempotent_within_same_day() -> None:
    expander = _ScriptedExpander("今天發生了一件事。")
    service, _, _, _ = await _wire(
        seed_texts=["事件種子"], expander=expander,
    )

    character = _character()
    when = datetime(2026, 4, 20, 10, tzinfo=timezone.utc)

    first = await service.ensure_today(character, now=when)
    second = await service.ensure_today(character, now=when)

    assert first.newly_rolled == 1
    # Same day again — cached, no extra expansion call.
    assert second.newly_rolled == 0
    assert expander.call_count == 1


@pytest.mark.asyncio
async def test_ensure_today_handles_empty_seed_pool_gracefully() -> None:
    expander = _ScriptedExpander()
    service, _, _, _ = await _wire(seed_texts=[], expander=expander)

    report = await service.ensure_today(
        _character(), now=datetime(2026, 4, 20, tzinfo=timezone.utc),
    )
    assert report.newly_rolled == 0
    assert report.events == ()
    assert expander.call_count == 0


@pytest.mark.asyncio
async def test_ensure_today_uses_owner_timezone_for_civil_day() -> None:
    expander = _ScriptedExpander("午夜後的故事。")
    service, _, events_repo, _ = await _wire(
        seed_texts=["午夜後的事件"],
        expander=expander,
        operator_profile_service=_OperatorProfileService(),
    )

    character = replace(_character(), user_id="owner-tw")
    report = await service.ensure_today(
        character,
        now=datetime(2026, 6, 14, 16, 30, tzinfo=timezone.utc),
    )

    assert report.newly_rolled == 1
    assert await events_repo.get_for_day(character.id, "2026-06-14") == []
    stored = await events_repo.get_for_day(character.id, "2026-06-15")
    assert len(stored) == 1
