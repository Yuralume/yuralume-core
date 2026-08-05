"""Integration: arc beat staging → StoryEvent via post-turn realization.

When the character has an active arc with a beat due today, the daily
``ensure_today`` call must:

1. Stage that beat for the prompt and record play-attempt facts.
2. Not materialize a StoryEvent until post-turn marks the beat realized.
3. Keep gacha from hijacking the due arc beat's daily slot.

When no arc beat is due, the gacha path runs as before.

**Unattended callers** (proactive tick, character warm-up) pass
``unattended=True``. Everything above still holds for them, with one
exception: a beat whose ``operator_position`` is ``central`` is a scene
about the player, so it is left waiting instead of being attempted and
— past the recheck threshold — written into canon while the player is
away (ARC_PLAYER_POSITION_PLAN §2 #5, red line 4). The attended chat
path is unchanged for every position, which is what the paired
characterization tests below pin.
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
    BEAT_PENDING,
    BEAT_REALIZED,
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
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

    def __init__(
        self,
        today: date,
        *,
        tension: str = TENSION_SETUP,
        operator_position: str | None = None,
    ) -> None:
        self._today = today
        self._tension = tension
        self._operator_position = operator_position

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
            operator_position=self._operator_position,
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
    operator_position: str | None = None,
):
    seed_repo = InMemoryStorySeedRepository()
    event_repo = InMemoryStoryEventRepository()
    memory_repo = InMemoryMemoryRepository()
    arc_repo = InMemoryStoryArcRepository()
    arc_service = StoryArcService(
        repository=arc_repo,
        planner=_FixedBeatPlanner(
            today, tension=tension, operator_position=operator_position,
        ),
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


# --- OP2-B follow-up: unattended callers must not play a central beat ---
#
# ``ensure_today`` has two kinds of caller. The chat turn is attended —
# a player is in the room and the due beat is about to become today's
# scene directive. The proactive tick and the character warm-up are not:
# nothing they do here reaches a player. Before ``unattended``, both used
# the same branch, so background ticks alone drove a ``central`` beat
# past the recheck threshold and the rechecker performed it into canon
# unseen. These tests pin both halves: central changes on the background
# path only, and nothing changes on the attended path at all.


def _realizing_rechecker() -> _FixedBeatRechecker:
    return _FixedBeatRechecker(
        StoryBeatRecheckDecision(
            action="mark_realized",
            reason="互動已完成 beat",
            narrative="我終於把今天要說的話說出口了。",
        ),
    )


@pytest.mark.asyncio
async def test_unattended_ensure_today_leaves_central_beat_waiting() -> None:
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    event_service, arc_service, arc_repo, expander, event_repo, seed_repo, _ = (
        _services(today, operator_position=OPERATOR_POSITION_CENTRAL)
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]
    # A stocked gacha pool: passing over the beat must not fall through
    # to a diary entry either. The beat still owns the day — see
    # ``test_player_arriving_after_unattended_ticks_still_gets_the_beat``
    # for what a roll here would cost.
    await seed_repo.add(StorySeed.create(seed_text="午後的慢跑"))

    report = await event_service.ensure_today(
        character, now=now, unattended=True,
    )

    assert report.newly_rolled == 0
    assert report.events == ()
    assert await event_repo.get_for_day(character.id, today.isoformat()) == []
    assert expander.calls == []
    updated = await arc_repo.get(arc.id)
    assert updated is not None
    waiting = updated.find_beat(beat.id)
    assert waiting is not None
    assert waiting.status == BEAT_PENDING
    assert waiting.play_attempt_count == 0
    assert waiting.last_play_attempt_source is None
    assert waiting.realized_event_id is None


@pytest.mark.asyncio
async def test_unattended_ticks_never_realize_a_central_beat() -> None:
    """Red line 4: repeat background ticks must not reach the rechecker.

    The threshold is small, so before the fix a couple of ticks were
    enough — this is the exact path that performed the player's own
    scene while they were away.
    """
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    rechecker = _realizing_rechecker()
    event_service, arc_service, arc_repo, _, event_repo, _, memory_repo = (
        _services(
            today,
            rechecker=rechecker,
            operator_position=OPERATOR_POSITION_CENTRAL,
        )
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    for _ in range(5):
        report = await event_service.ensure_today(
            character, now=now, unattended=True,
        )
        assert report.newly_rolled == 0

    assert rechecker.contexts == []
    assert await event_repo.get_for_day(character.id, today.isoformat()) == []
    updated = await arc_repo.get(arc.id)
    assert updated is not None
    still_waiting = updated.find_beat(beat.id)
    assert still_waiting is not None
    assert still_waiting.status == BEAT_PENDING
    assert still_waiting.play_attempt_count == 0
    assert await memory_repo.query(character.id) == []


@pytest.mark.asyncio
async def test_player_arriving_after_unattended_ticks_still_gets_the_beat() -> None:
    """Passing over is a pause, not a consumption.

    Also pins *why* passing over holds the day's slot rather than
    falling through to gacha: the pool below is stocked, so a fall-
    through would roll a diary entry, and ``ensure_today`` returns at
    the top once the day is full — the attended call would then never
    reach the arc branch, never record its attempt, and the beat could
    never reach the rechecker even with the player present. The
    attempt count below is 1 exactly because the ticks left the day
    open.
    """
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    event_service, arc_service, arc_repo, _, _, seed_repo, _ = _services(
        today, operator_position=OPERATOR_POSITION_CENTRAL,
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]
    await seed_repo.add(StorySeed.create(seed_text="午後的慢跑"))

    for _ in range(3):
        await event_service.ensure_today(character, now=now, unattended=True)
    await event_service.ensure_today(character, now=now)

    updated = await arc_repo.get(arc.id)
    assert updated is not None
    staged = updated.find_beat(beat.id)
    assert staged is not None
    assert staged.play_attempt_count == 1
    assert staged.last_play_attempt_source == "chat_scene_directive"


@pytest.mark.asyncio
async def test_attended_ensure_today_still_stages_a_central_beat() -> None:
    """Characterization: the chat path is untouched by this fix.

    Identical assertions to
    ``test_arc_beat_is_staged_not_materialized_on_due_date``, with the
    beat marked ``central`` — the player *is* in the room, so this is
    exactly the surface the beat was waiting for.
    """
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    event_service, arc_service, arc_repo, expander, event_repo, *_ = _services(
        today, operator_position=OPERATOR_POSITION_CENTRAL,
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    report = await event_service.ensure_today(character, now=now)

    assert report.newly_rolled == 0
    assert report.events == ()
    assert await event_repo.get_for_day(character.id, today.isoformat()) == []
    assert expander.calls == []
    updated = await arc_repo.get(arc.id)
    assert updated is not None
    staged = updated.find_beat(beat.id)
    assert staged is not None
    assert staged.status == BEAT_PENDING
    assert staged.play_attempt_count == 1
    assert staged.last_play_attempt_source == "chat_scene_directive"


@pytest.mark.asyncio
async def test_attended_recheck_can_still_realize_a_central_beat() -> None:
    """The rechecker keeps its say on the attended path.

    ``central`` restricts *who may play the beat*, not what the
    rechecker may conclude once a player was actually given it and did
    not take it.
    """
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    rechecker = _realizing_rechecker()
    event_service, arc_service, arc_repo, _, event_repo, _, _ = _services(
        today,
        rechecker=rechecker,
        operator_position=OPERATOR_POSITION_CENTRAL,
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    await event_service.ensure_today(character, now=now)
    second = await event_service.ensure_today(character, now=now)

    assert second.newly_rolled == 1
    assert len(rechecker.contexts) == 1
    events = await event_repo.get_for_day(character.id, today.isoformat())
    assert [event.arc_beat_id for event in events] == [beat.id]
    updated = await arc_repo.get(arc.id)
    assert updated is not None
    realized = updated.find_beat(beat.id)
    assert realized is not None
    assert realized.status == BEAT_REALIZED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "position",
    [None, OPERATOR_POSITION_ABSENT, OPERATOR_POSITION_PRESENT],
)
async def test_unattended_ensure_today_still_stages_other_positions(
    position: str | None,
) -> None:
    """Only ``central`` is passed over — the other three are unchanged.

    ``None`` is in here on purpose: every beat written before OP0 reads
    back as unjudged, and treating "nobody has said" as "the player is
    essential" would freeze every existing arc's background progress.
    """
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    event_service, arc_service, arc_repo, _, _, _, _ = _services(
        today, operator_position=position,
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    report = await event_service.ensure_today(
        character, now=now, unattended=True,
    )

    assert report.newly_rolled == 0
    updated = await arc_repo.get(arc.id)
    assert updated is not None
    staged = updated.find_beat(beat.id)
    assert staged is not None
    assert staged.play_attempt_count == 1
    assert staged.last_play_attempt_source == "chat_scene_directive"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "position",
    [None, OPERATOR_POSITION_ABSENT, OPERATOR_POSITION_PRESENT],
)
async def test_unattended_recheck_still_realizes_other_positions(
    position: str | None,
) -> None:
    today = date(2026, 5, 10)
    now = datetime(2026, 5, 10, 10, 0, tzinfo=timezone.utc)
    rechecker = _realizing_rechecker()
    event_service, arc_service, arc_repo, _, event_repo, _, _ = _services(
        today, rechecker=rechecker, operator_position=position,
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    await event_service.ensure_today(character, now=now, unattended=True)
    second = await event_service.ensure_today(
        character, now=now, unattended=True,
    )

    assert second.newly_rolled == 1
    events = await event_repo.get_for_day(character.id, today.isoformat())
    assert [event.arc_beat_id for event in events] == [beat.id]
    updated = await arc_repo.get(arc.id)
    assert updated is not None
    realized = updated.find_beat(beat.id)
    assert realized is not None
    assert realized.status == BEAT_REALIZED
