from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_beat_scene_service import (
    StoryBeatSceneService,
)
from kokoro_link.application.services.story_event_service import StoryEventService
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.contracts.story import StoryEventExpanderPort
from kokoro_link.contracts.story_arc import (
    StoryArcPlannerPort,
    StoryBeatSceneDraft,
    StoryBeatSceneWriterPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    BEAT_REALIZED,
    StoryArc,
    StoryArcBeat,
    TENSION_RISING,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.companion import CharacterCompanion
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
    InMemoryStorySeedRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)


class _UnusedExpander(StoryEventExpanderPort):
    async def expand(self, **kwargs):  # noqa: ANN003
        raise AssertionError("C scene path must not use the diary expander")


class _FixedPlanner(StoryArcPlannerPort):
    def __init__(self, today: date) -> None:
        self._today = today

    async def plan_arc(
        self,
        *,
        character: Character,
        start_date: date,
        duration_days: int = 21,
        beat_count_hint: int = 5,
        hint: str | None = None,
        recent_dialogue_summary: str = "",
        operator_primary_language: str = "zh-TW",
    ) -> StoryArc:
        arc = StoryArc.create(
            character_id=character.id,
            title="試鏡週",
            premise="她準備面對一場重要試鏡。",
            theme="ambition",
            tone="dramatic",
            start_date=start_date,
            end_date=start_date + timedelta(days=duration_days),
        )
        beat = StoryArcBeat.create(
            arc_id=arc.id,
            sequence=0,
            scheduled_date=self._today,
            title="指導老師的最後提醒",
            summary="她在上台前聽見老師提醒自己別再逃避。",
            tension=TENSION_RISING,
            scene_characters=("指導老師",),
            location="排練室門口",
            dramatic_question="她敢不敢承認自己其實很想贏？",
            required=True,
        )
        return arc.with_beats([beat])


class _RecordingWriter(StoryBeatSceneWriterPort):
    def __init__(
        self,
        draft: StoryBeatSceneDraft | None = None,
    ) -> None:
        self.draft = draft or StoryBeatSceneDraft(
            narrative="我在排練室門口停下，指導老師沒有替我加油，只問我還要逃到什麼時候。",
            emotional_tone="tense",
            cast_strategy="npc_dialogue",
            participation_note="used NPC label; user not required",
        )
        self.contexts = []

    async def write_scene(self, context):
        self.contexts.append(context)
        return self.draft


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="想成為演員的學生。",
        personality=[],
        interests=[],
        speaking_style="坦率但有點逞強",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
        companions=(
            CharacterCompanion.create(
                name="指導老師",
                role="指導老師",
                brief_profile="嚴厲但看得見她的努力",
                relationship_snippet="總是在她逃避時把話說破",
            ),
        ),
    )


def _services(today: date, writer: StoryBeatSceneWriterPort):
    arc_repo = InMemoryStoryArcRepository()
    event_repo = InMemoryStoryEventRepository()
    memory_repo = InMemoryMemoryRepository()
    arc_service = StoryArcService(
        repository=arc_repo,
        planner=_FixedPlanner(today),
        local_tz=timezone.utc,
    )
    event_service = StoryEventService(
        gacha=StoryGachaService(
            seed_repository=InMemoryStorySeedRepository(),
            event_repository=event_repo,
        ),
        expander=_UnusedExpander(),
        event_repository=event_repo,
        memory_repository=memory_repo,
        local_tz=timezone.utc,
        arc_service=arc_service,
    )
    scene_service = StoryBeatSceneService(
        story_arc_service=arc_service,
        story_event_service=event_service,
        writer=writer,
        local_tz=timezone.utc,
    )
    return scene_service, arc_service, arc_repo, event_repo, memory_repo


@pytest.mark.asyncio
async def test_play_beat_writes_scene_event_memory_and_realizes_beat() -> None:
    today = date(2026, 6, 1)
    now = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    writer = _RecordingWriter()
    scene_service, arc_service, arc_repo, event_repo, memory_repo = _services(
        today,
        writer,
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    event = await scene_service.play_beat(character, beat_id=beat.id, now=now)

    assert event is not None
    assert event.arc_beat_id == beat.id
    assert event.narrative == writer.draft.narrative
    assert event.emotional_tone == "tense"
    events = await event_repo.get_for_day(character.id, today.isoformat())
    assert [e.id for e in events] == [event.id]
    memories = await memory_repo.query(character.id)
    # Realizing this single-beat arc both memorializes the scene narrative
    # (episodic) and completes the arc, which writes an arc-completion
    # milestone memory. query() sorts by created_at desc, so the milestone
    # (written last) comes first; the scene narrative is the episodic write.
    contents = [m.content for m in memories]
    assert writer.draft.narrative in contents
    milestone = next(m for m in memories if "arc_completion" in m.tags)
    assert milestone.content == f"我們一起走完了《{arc.title}》：{beat.title}：{beat.summary}"
    assert contents == [milestone.content, writer.draft.narrative]
    updated_arc = await arc_repo.get(arc.id)
    assert updated_arc is not None
    updated_beat = updated_arc.find_beat(beat.id)
    assert updated_beat is not None
    assert updated_beat.status == BEAT_REALIZED
    assert updated_beat.realized_event_id == event.id
    assert updated_beat.last_play_attempt_source == "scene_simulation"
    assert updated_beat.last_play_attempt_result == "realized"


@pytest.mark.asyncio
async def test_play_beat_passes_no_user_policy_and_companion_context() -> None:
    today = date(2026, 6, 1)
    writer = _RecordingWriter()
    scene_service, arc_service, *_ = _services(today, writer)
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    await scene_service.play_beat(
        character,
        beat_id=beat.id,
        now=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
        user_involvement_policy="使用者不在場，請讓老師和角色自己完成。",
    )

    assert writer.contexts
    context = writer.contexts[0]
    assert context.beat.scene_characters == ("指導老師",)
    assert context.character.companions[0].name == "指導老師"
    assert "使用者不在場" in context.user_involvement_policy


@pytest.mark.asyncio
async def test_play_beat_empty_scene_records_attempt_without_event() -> None:
    today = date(2026, 6, 1)
    scene_service, arc_service, arc_repo, event_repo, memory_repo = _services(
        today,
        _RecordingWriter(StoryBeatSceneDraft(narrative="")),
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    event = await scene_service.play_beat(
        character,
        beat_id=beat.id,
        now=datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc),
    )

    assert event is None
    assert await event_repo.get_for_day(character.id, today.isoformat()) == []
    assert await memory_repo.query(character.id) == []
    updated_arc = await arc_repo.get(arc.id)
    assert updated_arc is not None
    updated_beat = updated_arc.find_beat(beat.id)
    assert updated_beat is not None
    assert updated_beat.status != BEAT_REALIZED
    assert updated_beat.last_play_attempt_result == "empty_scene"
