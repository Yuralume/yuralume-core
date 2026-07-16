"""Integration: arc beat staging → StoryEvent via post-turn realization.

When the character has an active arc with a beat due today, the daily
``ensure_today`` call must:

1. Stage that beat for the prompt and record play-attempt facts.
2. Not materialize a StoryEvent until post-turn marks the beat realized.
3. Keep gacha from hijacking the due arc beat's daily slot.

When no arc beat is due, the gacha path runs as before.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_event_service import StoryEventService
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.contracts.story import StoryEventExpanderPort
from kokoro_link.contracts.story_arc import StoryArcPlannerPort
from kokoro_link.contracts.story_arc import (
    ArcCompletionMemoryContext,
    ArcCompletionMemoryDraft,
    ArcCompletionMemoryWriterPort,
    StoryBeatRecheckContext,
    StoryBeatRecheckDecision,
    StoryBeatRecheckerPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    BEAT_REALIZED,
    StoryArc,
    StoryArcBeat,
    TENSION_CLIMAX,
    TENSION_SETUP,
)
from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
    InMemoryStorySeedRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository


class _RecordingExpander(StoryEventExpanderPort):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.last_scene = None

    async def expand(
        self, *, seed, character_name, character_summary, speaking_style,
        world_frame, scene=None, character=None,
    ):
        self.calls.append(seed.seed_text)
        # Capture the scene context the service hands us so a regression
        # in the arc-beat → expander wiring (Phase 1) shows up here.
        self.last_scene = scene
        return (f"展開：{seed.seed_text}", "peaceful")


class _FixedBeatPlanner(StoryArcPlannerPort):
    """Planner that always produces exactly one beat on today's date."""

    def __init__(self, today: date, *, tension: str = TENSION_SETUP) -> None:
        self._today = today
        self._tension = tension

    async def plan_arc(
        self,
        *,
        character: Character,
        start_date: date,
        duration_days: int = 21,
        beat_count_hint: int = 5,
        hint: str | None = None,
        recent_dialogue_summary: str = "",
    ) -> StoryArc:
        arc = StoryArc.create(
            character_id=character.id,
            title="test arc",
            premise="setup premise",
            theme="custom",
            start_date=start_date,
            end_date=start_date + timedelta(days=duration_days),
        )
        beat = StoryArcBeat.create(
            arc_id=arc.id, sequence=0,
            scheduled_date=self._today,
            title="today beat", summary="今天要發生的事",
            tension=self._tension,
        )
        return arc.with_beats([beat])


class _FixedBeatRechecker(StoryBeatRecheckerPort):
    def __init__(self, decision: StoryBeatRecheckDecision) -> None:
        self.decision = decision
        self.contexts: list[StoryBeatRecheckContext] = []

    async def recheck(
        self,
        context: StoryBeatRecheckContext,
    ) -> StoryBeatRecheckDecision:
        self.contexts.append(context)
        return self.decision


class _FixedCompletionMemoryWriter(ArcCompletionMemoryWriterPort):
    def __init__(self, content: str) -> None:
        self.content = content
        self.contexts: list[ArcCompletionMemoryContext] = []

    async def write_memory(
        self,
        context: ArcCompletionMemoryContext,
    ) -> ArcCompletionMemoryDraft:
        self.contexts.append(context)
        return ArcCompletionMemoryDraft(content=self.content)


def _character() -> Character:
    return Character.create(
        name="Yui", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )


def _services(
    today: date,
    *,
    tension: str = TENSION_SETUP,
    rechecker: StoryBeatRecheckerPort | None = None,
    completion_writer: ArcCompletionMemoryWriterPort | None = None,
):
    seed_repo = InMemoryStorySeedRepository()
    event_repo = InMemoryStoryEventRepository()
    memory_repo = InMemoryMemoryRepository()
    arc_repo = InMemoryStoryArcRepository()
    arc_service = StoryArcService(
        repository=arc_repo,
        planner=_FixedBeatPlanner(today, tension=tension),
        beat_rechecker=rechecker,
    )
    expander = _RecordingExpander()
    gacha = StoryGachaService(
        seed_repository=seed_repo, event_repository=event_repo,
    )
    event_service = StoryEventService(
        gacha=gacha,
        expander=expander,
        event_repository=event_repo,
        memory_repository=memory_repo,
        embedder=None,
        local_tz=timezone.utc,
        arc_service=arc_service,
        arc_completion_memory_writer=completion_writer,
    )
    return (
        event_service,
        arc_service,
        arc_repo,
        expander,
        event_repo,
        seed_repo,
        memory_repo,
    )


@pytest.mark.asyncio
async def test_arc_beat_is_staged_not_materialized_on_due_date() -> None:
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    event_service, arc_service, arc_repo, expander, event_repo, *_ = _services(today)
    character = _character()

    # Prime: create arc with today's beat.
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    report = await event_service.ensure_today(character, now=now)

    assert report.newly_rolled == 0
    assert report.events == ()
    assert await event_repo.get_for_day(character.id, today.isoformat()) == []
    assert expander.calls == []
    updated_arc = await arc_repo.get(arc.id)
    assert updated_arc is not None
    updated_beat = updated_arc.find_beat(beat.id)
    assert updated_beat is not None
    assert updated_beat.status != BEAT_REALIZED
    assert updated_beat.realized_event_id is None
    assert updated_beat.play_attempt_count == 1
    assert updated_beat.last_play_attempt_source == "chat_scene_directive"


@pytest.mark.asyncio
async def test_due_arc_beat_blocks_gacha_until_it_is_performed() -> None:
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    event_service, arc_service, _, expander, event_repo, seed_repo, _ = _services(today)
    character = _character()
    await arc_service.start_new_arc(character, today=today)
    await seed_repo.add(StorySeed.create(seed_text="午後的慢跑"))

    first = await event_service.ensure_today(character, now=now)
    second = await event_service.ensure_today(character, now=now)

    assert first.newly_rolled == 0
    assert second.newly_rolled == 0
    assert expander.calls == []
    events = await event_repo.get_for_day(character.id, today.isoformat())
    assert events == []


@pytest.mark.asyncio
async def test_repeated_arc_beat_recheck_can_realize_event() -> None:
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    rechecker = _FixedBeatRechecker(
        StoryBeatRecheckDecision(
            action="mark_realized",
            reason="互動已完成 beat",
            narrative="我終於把今天要說的話說出口了。",
        ),
    )
    event_service, arc_service, arc_repo, _, event_repo, _, memory_repo = (
        _services(today, rechecker=rechecker)
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    first = await event_service.ensure_today(character, now=now)
    second = await event_service.ensure_today(character, now=now)

    assert first.events == ()
    assert second.newly_rolled == 1
    assert len(second.events) == 1
    assert second.events[0].narrative == "我終於把今天要說的話說出口了。"
    assert len(rechecker.contexts) == 1
    assert rechecker.contexts[0].beat.play_attempt_count == 2
    events = await event_repo.get_for_day(character.id, today.isoformat())
    assert [event.arc_beat_id for event in events] == [beat.id]
    updated = await arc_repo.get(arc.id)
    assert updated is not None
    realized = updated.find_beat(beat.id)
    assert realized is not None
    assert realized.status == BEAT_REALIZED
    memories = await memory_repo.query(character.id)
    assert any(m.content == "我終於把今天要說的話說出口了。" for m in memories)


@pytest.mark.asyncio
async def test_record_arc_beat_realization_writes_event_memory_and_status() -> None:
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    event_service, arc_service, arc_repo, expander, event_repo, _, memory_repo = (
        _services(today)
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    event = await event_service.record_arc_beat_realization(
        character,
        beat_id=beat.id,
        narrative="我真的把那場關鍵對話說出口了。",
        now=now,
    )

    assert event is not None
    assert event.arc_beat_id == beat.id
    assert event.seed_id is None
    assert expander.calls == []
    events = await event_repo.get_for_day(character.id, today.isoformat())
    assert [e.id for e in events] == [event.id]
    updated_arc = await arc_repo.get(arc.id)
    assert updated_arc is not None
    updated_beat = updated_arc.find_beat(beat.id)
    assert updated_beat is not None
    assert updated_beat.status == BEAT_REALIZED
    assert updated_beat.realized_event_id == event.id
    memories = await memory_repo.query(character.id)
    assert len(memories) == 2
    assert any(m.content == "我真的把那場關鍵對話說出口了。" for m in memories)
    assert any("arc_completion" in m.tags for m in memories)


@pytest.mark.asyncio
async def test_climax_arc_beat_realization_writes_milestone_memory() -> None:
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    event_service, arc_service, _, _, _, _, memory_repo = _services(
        today,
        tension=TENSION_CLIMAX,
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    await event_service.record_arc_beat_realization(
        character,
        beat_id=beat.id,
        narrative="我終於把最重要的話說出口了。",
        now=now,
    )

    memories = await memory_repo.query(character.id)
    arc_memory = next(m for m in memories if "arc_milestone" in m.tags)
    completion_memory = next(m for m in memories if "arc_completion" in m.tags)
    assert arc_memory.kind == MemoryKind.RELATIONSHIP_MILESTONE
    assert arc_memory.salience == pytest.approx(0.9)
    assert completion_memory.kind == MemoryKind.RELATIONSHIP_MILESTONE
    assert completion_memory.salience == pytest.approx(0.95)


@pytest.mark.asyncio
async def test_arc_completion_memory_prefers_writer_content() -> None:
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    writer = _FixedCompletionMemoryWriter(
        "我們記得這段故事真正收束時，她沒有再逃避那個舞台。",
    )
    event_service, arc_service, _, _, _, _, memory_repo = _services(
        today,
        completion_writer=writer,
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    await event_service.record_arc_beat_realization(
        character,
        beat_id=beat.id,
        narrative="我終於把那場關鍵對話說出口了。",
        now=now,
    )

    memories = await memory_repo.query(character.id)
    completion = next(m for m in memories if "arc_completion" in m.tags)
    assert completion.content == "我們記得這段故事真正收束時，她沒有再逃避那個舞台。"
    assert len(writer.contexts) == 1
    assert writer.contexts[0].realized_beats[0].id == beat.id


@pytest.mark.asyncio
async def test_no_due_beat_falls_back_to_gacha() -> None:
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    # Planner puts the beat in the future → no beat due today.
    future_today = today + timedelta(days=3)
    event_service, arc_service, _, expander, _, seed_repo, _ = _services(future_today)
    character = _character()

    # Seed the gacha pool.
    seed = StorySeed.create(seed_text="午後的慢跑", tags=("exercise",))
    await seed_repo.add(seed)

    # Start arc (beat is 3 days in future).
    await arc_service.start_new_arc(character, today=future_today)

    report = await event_service.ensure_today(character, now=now)

    # Gacha ran (seed text expanded), arc did not contribute.
    assert report.newly_rolled == 1
    event = report.events[0]
    assert event.seed_id == seed.id
    assert event.arc_beat_id is None
