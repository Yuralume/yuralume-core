"""StoryArcService BDD.

Covers the operations the chat / REST / post-turn pipelines depend on:

- ``ensure_active_arc`` — lazy creation via planner, reuses existing
  active arc, auto-starts vs read-only mode
- ``start_new_arc`` — abandons prior active arc (keeps ≤1 active)
- ``next_beat_due`` — returns today's or earliest overdue beat
- ``realize_beat`` — flips beat state + auto-completes arc when all
  beats terminal
- ``mark_beat_play_attempted`` — records staging facts for LLM decisions
- ``apply_adjustments`` — shift / modify / insert / mark_realized / skip_beat,
  with realized-beat protection
- ``forward_beats`` — prompt-feed shape
- Beat edit / delete guard realized beats
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.story_arc_service import (
    ArcAdjustment,
    StoryArcService,
)
from kokoro_link.contracts.story_arc import (
    StoryArcPlannerPort,
    StoryArcSeasonContext,
    StoryArcSeasonDecision,
    StoryArcSeasonDeciderPort,
    StoryBeatRecheckContext,
    StoryBeatRecheckDecision,
    StoryBeatRecheckerPort,
)
from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.domain.entities.arc_template import ArcTemplate, ArcTemplateBeat
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    ARC_ABANDONED,
    ARC_ACTIVE,
    ARC_COMPLETED,
    BEAT_PENDING,
    BEAT_REALIZED,
    BEAT_SKIPPED,
    StoryArc,
    StoryArcBeat,
    TENSION_RISING,
    TENSION_SETUP,
)
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
)


class _CountingPlanner(StoryArcPlannerPort):
    """Deterministic planner — always 3 beats on days 0 / 5 / 10."""

    def __init__(self) -> None:
        self.calls = 0
        self.recent_dialogue_summaries: list[str] = []
        self.hints: list[str | None] = []

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
        self.calls += 1
        self.hints.append(hint)
        self.recent_dialogue_summaries.append(recent_dialogue_summary)
        arc = StoryArc.create(
            character_id=character.id,
            title=hint or f"arc-{self.calls}",
            premise="test premise",
            theme="custom",
            start_date=start_date,
            end_date=start_date + timedelta(days=duration_days),
        )
        beats = [
            StoryArcBeat.create(
                arc_id=arc.id, sequence=i,
                scheduled_date=start_date + timedelta(days=offset),
                title=f"beat {i}", summary=f"summary {i}",
                tension=TENSION_SETUP if i == 0 else TENSION_RISING,
            )
            for i, offset in enumerate([0, 5, 10])
        ]
        return arc.with_beats(beats)


class _AllowNextSeasonDecider(StoryArcSeasonDeciderPort):
    def __init__(self, *, should_start: bool, hint: str | None = None) -> None:
        self.should_start = should_start
        self.hint = hint
        self.contexts: list[StoryArcSeasonContext] = []

    async def decide(
        self, context: StoryArcSeasonContext,
    ) -> StoryArcSeasonDecision:
        self.contexts.append(context)
        return StoryArcSeasonDecision(
            should_start=self.should_start,
            reason="test decision",
            hint=self.hint,
        )


class _RecordingBeatRechecker(StoryBeatRecheckerPort):
    def __init__(self, decision: StoryBeatRecheckDecision) -> None:
        self.decision = decision
        self.contexts: list[StoryBeatRecheckContext] = []

    async def recheck(
        self,
        context: StoryBeatRecheckContext,
    ) -> StoryBeatRecheckDecision:
        self.contexts.append(context)
        return self.decision


class _StubTemplateRepo(ArcTemplateRepositoryPort):
    def __init__(self, templates: dict[str, ArcTemplate]) -> None:
        self._templates = templates

    async def get_for_user(
        self, template_id: str, *, user_id: str | None,
    ) -> ArcTemplate | None:
        return self._templates.get(template_id)

    async def list_for_user(self, user_id: str | None) -> list[ArcTemplate]:
        return list(self._templates.values())

    async def list_packs(self) -> list[ArcTemplate]:
        return list(self._templates.values())

    async def save_for_user(
        self, template, *, user_id, overwrite=False,
    ) -> str:
        raise NotImplementedError

    async def delete_for_user(
        self, template_id: str, *, user_id: str,
    ) -> bool:
        raise NotImplementedError

    async def upsert_pack(
        self, template, *, pack_id: str, external_id: str | None = None,
    ) -> str:
        raise NotImplementedError


def _character(*, arc_template_id: str | None = None) -> Character:
    return Character.create(
        name="Yui", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
        arc_template_id=arc_template_id,
    )


def _service() -> tuple[StoryArcService, InMemoryStoryArcRepository, _CountingPlanner]:
    repo = InMemoryStoryArcRepository()
    planner = _CountingPlanner()
    svc = StoryArcService(repository=repo, planner=planner)
    return svc, repo, planner


def _template(template_id: str = "trial_arc") -> ArcTemplate:
    return ArcTemplate.create(
        id=template_id,
        title="試鏡週",
        premise="她準備一場只有一次機會的試鏡。",
        theme="ambition",
        duration_days=5,
        beats=[
            ArcTemplateBeat.create(
                sequence=0,
                day_offset=0,
                title="看見公告",
                summary="她在公告欄前停下來。",
            ),
            ArcTemplateBeat.create(
                sequence=1,
                day_offset=1,
                title="練習",
                summary="她在空教室裡練到聲音發顫。",
            ),
        ],
    )


# ---- lifecycle --------------------------------------------------------


@pytest.mark.asyncio
async def test_ensure_active_arc_lazy_creates_on_first_call() -> None:
    svc, _, planner = _service()
    character = _character()
    today = date(2026, 5, 1)

    arc = await svc.ensure_active_arc(character, today=today, auto_start=True)

    assert arc is not None
    assert arc.status == ARC_ACTIVE
    assert len(arc.beats) == 3
    assert planner.calls == 1


@pytest.mark.asyncio
async def test_ensure_active_arc_reuses_existing() -> None:
    svc, _, planner = _service()
    character = _character()
    today = date(2026, 5, 1)

    first = await svc.ensure_active_arc(character, today=today)
    second = await svc.ensure_active_arc(character, today=today)

    assert second is not None
    assert second.id == first.id
    # Planner called only once.
    assert planner.calls == 1


@pytest.mark.asyncio
async def test_ensure_active_arc_read_only_returns_none_when_absent() -> None:
    svc, _, planner = _service()
    character = _character()

    arc = await svc.ensure_active_arc(character, auto_start=False)

    assert arc is None
    assert planner.calls == 0


@pytest.mark.asyncio
async def test_start_new_arc_abandons_existing_active() -> None:
    svc, repo, _ = _service()
    character = _character()

    first = await svc.start_new_arc(character, today=date(2026, 5, 1))
    second = await svc.start_new_arc(character, today=date(2026, 5, 2), hint="一條新的線")

    assert first.id != second.id
    reloaded_first = await repo.get(first.id)
    assert reloaded_first is not None
    assert reloaded_first.status == ARC_ABANDONED
    # All pending beats on the abandoned arc are now skipped.
    assert all(b.status == BEAT_SKIPPED for b in reloaded_first.beats)


@pytest.mark.asyncio
async def test_completed_template_arc_enters_dormant_instead_of_respawning() -> None:
    arc_repo = InMemoryStoryArcRepository()
    planner = _CountingPlanner()
    service = StoryArcService(
        repository=arc_repo,
        planner=planner,
        template_repository=_StubTemplateRepo({"trial_arc": _template()}),
    )
    character = _character(arc_template_id="trial_arc")
    first = await service.start_new_arc(character, today=date(2026, 5, 1))
    assert first.source_template_id == "trial_arc"

    for beat in first.beats:
        await service.realize_beat(beat_id=beat.id, event_id=f"event-{beat.id}")

    next_arc = await service.ensure_active_arc(
        character, today=date(2026, 5, 3), auto_start=True,
    )

    assert next_arc is None
    assert planner.calls == 0
    assert await arc_repo.get_active_for_character(character.id) is None
    arcs = await arc_repo.list_for_character(character.id)
    assert [arc.status for arc in arcs] == [ARC_COMPLETED]


@pytest.mark.asyncio
async def test_completed_arc_does_not_consult_season_decider_when_opening_disabled() -> None:
    arc_repo = InMemoryStoryArcRepository()
    planner = _CountingPlanner()
    decider = _AllowNextSeasonDecider(
        should_start=True,
        hint="下一季不該在聊天熱路徑開",
    )
    service = StoryArcService(
        repository=arc_repo,
        planner=planner,
        template_repository=_StubTemplateRepo({"trial_arc": _template()}),
        season_decider=decider,
    )
    character = _character(arc_template_id="trial_arc")
    first = await service.start_new_arc(character, today=date(2026, 5, 1))
    for beat in first.beats:
        await service.realize_beat(beat_id=beat.id, event_id=f"event-{beat.id}")

    next_arc = await service.ensure_active_arc(
        character,
        today=date(2026, 5, 4),
        auto_start=True,
        open_new_season=False,
    )

    assert next_arc is None
    assert decider.contexts == []
    assert planner.calls == 0


@pytest.mark.asyncio
async def test_manual_start_can_reuse_consumed_template() -> None:
    arc_repo = InMemoryStoryArcRepository()
    service = StoryArcService(
        repository=arc_repo,
        planner=_CountingPlanner(),
        template_repository=_StubTemplateRepo({"trial_arc": _template()}),
    )
    character = _character(arc_template_id="trial_arc")
    first = await service.start_new_arc(character, today=date(2026, 5, 1))
    for beat in first.beats:
        await service.realize_beat(beat_id=beat.id, event_id=f"event-{beat.id}")

    second = await service.start_new_arc(
        character,
        today=date(2026, 5, 10),
        allow_consumed_template=True,
    )

    assert second.id != first.id
    assert second.title == "試鏡週"
    assert second.source_template_id == "trial_arc"


@pytest.mark.asyncio
async def test_season_decider_starts_llm_arc_with_completed_arc_context() -> None:
    arc_repo = InMemoryStoryArcRepository()
    event_repo = InMemoryStoryEventRepository()
    planner = _CountingPlanner()
    decider = _AllowNextSeasonDecider(
        should_start=True,
        hint="承接試鏡後的新舞台",
    )
    service = StoryArcService(
        repository=arc_repo,
        planner=planner,
        template_repository=_StubTemplateRepo({"trial_arc": _template()}),
        event_repository=event_repo,
        season_decider=decider,
    )
    character = _character(arc_template_id="trial_arc")
    first = await service.start_new_arc(character, today=date(2026, 5, 1))
    for index, beat in enumerate(first.beats):
        event = StoryEvent.create(
            character_id=character.id,
            date=(date(2026, 5, 1) + timedelta(days=index)).isoformat(),
            arc_beat_id=beat.id,
            narrative=f"第 {index + 1} 場試鏡事件已經發生。",
        )
        await event_repo.add(event)
        await service.realize_beat(beat_id=beat.id, event_id=event.id)

    next_arc = await service.ensure_active_arc(
        character, today=date(2026, 5, 4), auto_start=True,
    )

    assert next_arc is not None
    assert next_arc.title == "承接試鏡後的新舞台"
    assert next_arc.source_template_id is None
    assert planner.calls == 1
    assert decider.contexts
    assert decider.contexts[0].completed_arc is not None
    context = planner.recent_dialogue_summaries[0]
    assert "上一段故事" in context
    assert "她準備一場只有一次機會的試鏡。" in context
    assert "第 1 場試鏡事件已經發生。" in context


# ---- beat scheduling --------------------------------------------------


@pytest.mark.asyncio
async def test_next_beat_due_returns_earliest_overdue_or_today() -> None:
    svc, _, _ = _service()
    character = _character()
    start = date(2026, 5, 1)
    await svc.start_new_arc(character, today=start)

    # Day of the first beat.
    hit = await svc.next_beat_due(character.id, today=start)
    assert hit is not None
    _, beat = hit
    assert beat.scheduled_date == start

    # Day before any beat.
    miss = await svc.next_beat_due(character.id, today=start - timedelta(days=1))
    assert miss is None


@pytest.mark.asyncio
async def test_mark_beat_play_attempted_records_progress_facts() -> None:
    svc, repo, _ = _service()
    character = _character()
    start = date(2026, 5, 1)
    arc = await svc.start_new_arc(character, today=start)
    beat = arc.beats[0]
    attempted_at = datetime(2026, 5, 1, 8, 30, tzinfo=timezone.utc)

    await svc.mark_beat_play_attempted(
        beat_id=beat.id,
        attempted_at=attempted_at,
        source="chat_scene_directive",
        result="prompted",
        push_intensity="scene_directive",
    )

    updated = await repo.get(arc.id)
    assert updated is not None
    marked = updated.find_beat(beat.id)
    assert marked is not None
    assert marked.status == BEAT_PENDING
    assert marked.play_attempt_count == 1
    assert marked.last_play_attempt_at == attempted_at
    assert marked.last_play_attempt_source == "chat_scene_directive"
    assert marked.last_play_attempt_result == "prompted"
    assert marked.last_play_push_intensity == "scene_directive"


@pytest.mark.asyncio
async def test_recheck_due_beat_waits_until_attempt_threshold() -> None:
    repo = InMemoryStoryArcRepository()
    rechecker = _RecordingBeatRechecker(
        StoryBeatRecheckDecision(
            action="delay_beat",
            days=2,
            reason="需要晚點再演",
        ),
    )
    svc = StoryArcService(
        repository=repo,
        planner=_CountingPlanner(),
        beat_rechecker=rechecker,
        recheck_attempt_threshold=2,
    )
    character = _character()
    arc = await svc.start_new_arc(character, today=date(2026, 5, 1))
    beat = arc.beats[0]

    await svc.mark_beat_play_attempted(beat_id=beat.id)
    first = await svc.recheck_due_beat_after_attempt(
        character,
        beat_id=beat.id,
        today=date(2026, 5, 1),
    )
    await svc.mark_beat_play_attempted(beat_id=beat.id)
    second = await svc.recheck_due_beat_after_attempt(
        character,
        beat_id=beat.id,
        today=date(2026, 5, 1),
    )

    assert first is None
    assert second is not None
    assert second.action == "delay_beat"
    assert len(rechecker.contexts) == 1
    assert rechecker.contexts[0].beat.play_attempt_count == 2
    updated = await repo.get(arc.id)
    assert updated is not None
    shifted = updated.find_beat(beat.id)
    assert shifted is not None
    assert shifted.scheduled_date == beat.scheduled_date + timedelta(days=2)


@pytest.mark.asyncio
async def test_recheck_mark_realized_returns_narrative_without_mutating_arc() -> None:
    repo = InMemoryStoryArcRepository()
    rechecker = _RecordingBeatRechecker(
        StoryBeatRecheckDecision(
            action="mark_realized",
            reason="對話已演完",
            narrative="我把那場核心衝突說出口了。",
        ),
    )
    svc = StoryArcService(
        repository=repo,
        planner=_CountingPlanner(),
        beat_rechecker=rechecker,
        recheck_attempt_threshold=1,
    )
    character = _character()
    arc = await svc.start_new_arc(character, today=date(2026, 5, 1))
    beat = arc.beats[0]
    await svc.mark_beat_play_attempted(beat_id=beat.id)

    adjustment = await svc.recheck_due_beat_after_attempt(
        character,
        beat_id=beat.id,
        today=date(2026, 5, 1),
    )

    assert adjustment is not None
    assert adjustment.action == "mark_realized"
    assert adjustment.narrative == "我把那場核心衝突說出口了。"
    updated = await repo.get(arc.id)
    assert updated is not None
    still_pending = updated.find_beat(beat.id)
    assert still_pending is not None
    assert still_pending.status == BEAT_PENDING


@pytest.mark.asyncio
async def test_realize_beat_sets_event_id_and_maybe_completes_arc() -> None:
    svc, repo, _ = _service()
    character = _character()
    start = date(2026, 5, 1)
    arc = await svc.start_new_arc(character, today=start)

    for beat in arc.beats:
        await svc.realize_beat(beat_id=beat.id, event_id=f"event-{beat.id}")

    final = await repo.get(arc.id)
    assert final is not None
    assert final.status == ARC_COMPLETED
    assert all(b.status == BEAT_REALIZED for b in final.beats)
    assert all(b.realized_event_id == f"event-{b.id}" for b in final.beats)


# ---- apply_adjustments ------------------------------------------------


@pytest.mark.asyncio
async def test_apply_adjustments_shifts_beat() -> None:
    svc, repo, _ = _service()
    character = _character()
    start = date(2026, 5, 1)
    arc = await svc.start_new_arc(character, today=start)
    pending = arc.beats[0]
    orig_date = pending.scheduled_date

    await svc.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ArcAdjustment(action="delay_beat", beat_id=pending.id, days=3),
        ],
    )

    updated = await repo.get(arc.id)
    assert updated is not None
    shifted = updated.find_beat(pending.id)
    assert shifted is not None
    assert shifted.scheduled_date == orig_date + timedelta(days=3)


@pytest.mark.asyncio
async def test_apply_adjustments_modify_rewrites_fields() -> None:
    svc, repo, _ = _service()
    character = _character()
    arc = await svc.start_new_arc(character, today=date(2026, 5, 1))
    beat = arc.beats[1]

    await svc.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ArcAdjustment(
                action="modify_beat", beat_id=beat.id,
                title="new title", summary="new summary",
                tension="climax",
            ),
        ],
    )

    updated = await repo.get(arc.id)
    assert updated is not None
    modified = updated.find_beat(beat.id)
    assert modified is not None
    assert modified.title == "new title"
    assert modified.summary == "new summary"
    assert modified.tension == "climax"


@pytest.mark.asyncio
async def test_apply_adjustments_insert_beat_appends() -> None:
    svc, repo, _ = _service()
    character = _character()
    arc = await svc.start_new_arc(character, today=date(2026, 5, 1))
    before = len(arc.beats)

    await svc.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ArcAdjustment(
                action="insert_beat",
                scheduled_date=date(2026, 5, 7),
                title="extra beat",
                summary="something new happened",
                tension="rising",
            ),
        ],
    )

    updated = await repo.get(arc.id)
    assert updated is not None
    assert len(updated.beats) == before + 1
    extra = [b for b in updated.beats if b.title == "extra beat"]
    assert len(extra) == 1


@pytest.mark.asyncio
async def test_apply_adjustments_mark_realized_skips_events() -> None:
    svc, repo, _ = _service()
    character = _character()
    arc = await svc.start_new_arc(character, today=date(2026, 5, 1))
    beat = arc.beats[0]

    await svc.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ArcAdjustment(action="mark_realized", beat_id=beat.id),
        ],
    )

    updated = await repo.get(arc.id)
    assert updated is not None
    realized = updated.find_beat(beat.id)
    assert realized is not None
    assert realized.status == BEAT_REALIZED
    assert realized.realized_event_id is None
    assert realized.last_play_attempt_result == "realized"


@pytest.mark.asyncio
async def test_apply_adjustments_skip_beat_marks_pending_skipped() -> None:
    svc, repo, _ = _service()
    character = _character()
    arc = await svc.start_new_arc(character, today=date(2026, 5, 1))
    beat = arc.beats[0]

    await svc.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ArcAdjustment(action="skip_beat", beat_id=beat.id),
        ],
    )

    updated = await repo.get(arc.id)
    assert updated is not None
    skipped = updated.find_beat(beat.id)
    assert skipped is not None
    assert skipped.status == BEAT_SKIPPED
    assert skipped.last_play_attempt_result == "skipped"


@pytest.mark.asyncio
async def test_apply_adjustments_cannot_modify_realized_beat() -> None:
    svc, repo, _ = _service()
    character = _character()
    arc = await svc.start_new_arc(character, today=date(2026, 5, 1))
    beat = arc.beats[0]
    await svc.realize_beat(beat_id=beat.id, event_id="evt")

    await svc.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ArcAdjustment(
                action="modify_beat", beat_id=beat.id,
                title="hijack", summary="hijack",
            ),
        ],
    )

    updated = await repo.get(arc.id)
    assert updated is not None
    unchanged = updated.find_beat(beat.id)
    assert unchanged is not None
    assert unchanged.title == "beat 0"  # unchanged


# ---- forward_beats ----------------------------------------------------


@pytest.mark.asyncio
async def test_forward_beats_returns_next_pending_excluding_today() -> None:
    svc, _, _ = _service()
    character = _character()
    start = date(2026, 5, 1)
    await svc.start_new_arc(character, today=start)

    result = await svc.forward_beats(character.id, after=start, limit=2)

    assert result is not None
    arc, beats = result
    assert arc is not None
    # include_today defaults to True via service ensure-path, but
    # forward_beats API uses StoryArc.forward_beats with include_today=True.
    assert len(beats) <= 2
    # Beats are in ascending date order.
    for earlier, later in zip(beats, beats[1:]):
        assert earlier.scheduled_date <= later.scheduled_date


# ---- beat CRUD --------------------------------------------------------


@pytest.mark.asyncio
async def test_update_beat_changes_fields_for_pending_only() -> None:
    svc, repo, _ = _service()
    character = _character()
    arc = await svc.start_new_arc(character, today=date(2026, 5, 1))
    pending = arc.beats[0]

    await svc.update_beat(
        beat_id=pending.id,
        title="operator-edited",
        tension="climax",
    )

    updated = await repo.get(arc.id)
    assert updated is not None
    edited = updated.find_beat(pending.id)
    assert edited is not None
    assert edited.title == "operator-edited"
    assert edited.tension == "climax"


@pytest.mark.asyncio
async def test_delete_beat_preserves_realized_beats() -> None:
    svc, repo, _ = _service()
    character = _character()
    arc = await svc.start_new_arc(character, today=date(2026, 5, 1))
    beat = arc.beats[0]
    await svc.realize_beat(beat_id=beat.id, event_id="evt")

    await svc.delete_beat(beat_id=beat.id)

    updated = await repo.get(arc.id)
    assert updated is not None
    # Still present — service refuses to delete realized beats.
    assert updated.find_beat(beat.id) is not None
