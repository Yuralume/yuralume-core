"""``StoryArcService.force_open_season`` — 起幕 layer 2's arc seam (SC1-B).

Two things are being pinned here at once:

* the new behaviour — a dormant character's next season opens *now*,
  through the same planner / series machinery the scheduled path uses,
  with only the decider's timing judgement removed;
* the old behaviour — chat, proactive and schedule still reach the
  decider exactly as before. A "skip the gate" seam is worth nothing if
  it quietly removed the gate for everyone, so those paths are
  characterized in the same file as the thing that could break them.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.contracts.story_arc import (
    StoryArcPlannerPort,
    StoryArcSeasonContext,
    StoryArcSeasonDecision,
    StoryArcSeasonDeciderPort,
)
from kokoro_link.domain.entities.arc_series import (
    SERIES_STATUS_ACTIVE,
    SERIES_STATUS_CONCLUDED,
    ArcSeries,
)
from kokoro_link.domain.entities.arc_template import ArcTemplate, ArcTemplateBeat
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    ARC_ABANDONED,
    ARC_COMPLETED,
    StoryArc,
    StoryArcBeat,
    TENSION_SETUP,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_arc_series import (
    InMemoryArcSeriesRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_templates import (
    InMemoryArcTemplateRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)


TODAY = date(2026, 6, 1)


# ── doubles ──────────────────────────────────────────────────────────


class _RecordingPlanner(StoryArcPlannerPort):
    def __init__(self) -> None:
        self.calls = 0
        self.hints: list[str | None] = []
        self.contexts: list[str] = []

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
        self.contexts.append(recent_dialogue_summary)
        arc = StoryArc.create(
            character_id=character.id,
            title=f"新的一季 {self.calls}",
            premise="她被推向一個沒預期的舞台",
            theme="custom",
            start_date=start_date,
            end_date=start_date + timedelta(days=duration_days),
        )
        beats = [
            StoryArcBeat.create(
                arc_id=arc.id,
                sequence=index,
                scheduled_date=start_date + timedelta(days=offset),
                title=f"第 {index} 顆",
                summary=f"第 {index} 顆的內容",
                tension=TENSION_SETUP,
            )
            for index, offset in enumerate((0, 5, 10))
        ]
        return arc.with_beats(beats)


class _RecordingDecider(StoryArcSeasonDeciderPort):
    def __init__(self, *, should_start: bool, hint: str | None = None) -> None:
        self._should_start = should_start
        self._hint = hint
        self.contexts: list[StoryArcSeasonContext] = []

    async def decide(
        self, context: StoryArcSeasonContext,
    ) -> StoryArcSeasonDecision:
        self.contexts.append(context)
        return StoryArcSeasonDecision(
            should_start=self._should_start,
            reason="test",
            hint=self._hint,
        )


def _character(**kwargs) -> Character:  # noqa: ANN003
    return Character.create(
        name="Mio",
        summary="a violinist",
        personality=[],
        interests=[],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        user_id="user-a",
        **kwargs,
    )


def _template(template_id: str, title: str) -> ArcTemplate:
    return ArcTemplate.create(
        id=template_id,
        title=title,
        premise=f"{title} 的劇情。",
        theme="growth",
        duration_days=7,
        beats=[
            ArcTemplateBeat.create(
                sequence=0, day_offset=0,
                title=f"{title} 開場", summary="第一場戲。",
            ),
        ],
    )


def _service(
    *, decider: _RecordingDecider | None = None,
) -> tuple[StoryArcService, InMemoryStoryArcRepository, _RecordingPlanner]:
    repo = InMemoryStoryArcRepository()
    planner = _RecordingPlanner()
    service = StoryArcService(
        repository=repo, planner=planner, season_decider=decider,
    )
    return service, repo, planner


async def _series_service(
    *, decider: _RecordingDecider | None = None,
) -> tuple[
    StoryArcService,
    InMemoryStoryArcRepository,
    InMemoryArcSeriesRepository,
    _RecordingPlanner,
    Character,
]:
    arcs = InMemoryStoryArcRepository()
    series = InMemoryArcSeriesRepository()
    templates = InMemoryArcTemplateRepository()
    planner = _RecordingPlanner()
    for template_id, title in (("book-one", "第一本"), ("book-two", "第二本")):
        await templates.save_for_user(
            _template(template_id, title), user_id="user-a",
        )
    await series.save_for_user(
        ArcSeries.create(
            id="series-a",
            title="連載篇",
            premise="兩本劇本依序展開。",
            template_ids=["book-one", "book-two"],
            user_id="user-a",
        ),
        user_id="user-a",
    )
    service = StoryArcService(
        repository=arcs,
        planner=planner,
        template_repository=templates,
        series_repository=series,
        season_decider=decider,
    )
    return service, arcs, series, planner, _character(arc_series_id="series-a")


# ── non-series: the decider's timing call is the only thing removed ──


@pytest.mark.asyncio
async def test_force_open_plans_a_season_the_decider_would_have_refused(
) -> None:
    decider = _RecordingDecider(should_start=False)
    service, repo, planner = _service(decider=decider)
    character = _character()
    first = await service.start_new_arc(character, today=TODAY)
    for beat in first.beats:
        await service.realize_beat(beat_id=beat.id, event_id=None)
    # the scheduled path defers, because the decider said not yet …
    assert await service.ensure_active_arc(character, today=TODAY) is None
    planned_before = planner.calls
    consulted_before = len(decider.contexts)

    arc = await service.force_open_season(character, today=TODAY)

    # … and the button opens the season anyway, without asking again.
    assert arc is not None
    assert planner.calls == planned_before + 1
    assert len(decider.contexts) == consulted_before
    active = await repo.get_active_for_character(character.id)
    assert active is not None and active.id == arc.id


@pytest.mark.asyncio
async def test_forced_season_still_carries_the_previous_season_forward(
) -> None:
    """「沿用既有 planner 全鏈（含上一季 continuation context）」(§3.1).

    Skipping the decider must not skip the *material* the decider was
    reading — a forced season that started cold would restart the
    character's story rather than continue it.
    """
    service, _, planner = _service(decider=_RecordingDecider(should_start=False))
    character = _character()
    first = await service.start_new_arc(character, today=TODAY)
    for beat in first.beats:
        await service.realize_beat(beat_id=beat.id, event_id=None)

    await service.force_open_season(character, today=TODAY)

    continuation = planner.contexts[-1]
    assert "上一段故事" in continuation
    assert first.title in continuation
    assert first.premise in continuation


@pytest.mark.asyncio
async def test_force_open_works_with_no_decider_wired_at_all() -> None:
    """A missing decider is "nobody can say yes" — not "the answer is no"."""
    service, _, planner = _service(decider=None)
    character = _character()
    first = await service.start_new_arc(character, today=TODAY)
    for beat in first.beats:
        await service.realize_beat(beat_id=beat.id, event_id=None)
    assert await service.ensure_active_arc(character, today=TODAY) is None

    arc = await service.force_open_season(character, today=TODAY)

    assert arc is not None
    assert planner.calls == 2


@pytest.mark.asyncio
async def test_force_open_starts_the_very_first_season() -> None:
    """A character who has never had an arc still gets one from the button."""
    service, repo, planner = _service()

    arc = await service.force_open_season(_character(), today=TODAY)

    assert arc is not None
    assert planner.calls == 1
    assert await repo.get_active_for_character(arc.character_id) is not None


@pytest.mark.asyncio
async def test_force_open_refuses_to_torch_a_running_season() -> None:
    """A live arc falls through to the side story, it is not abandoned.

    Layer 1 has already had first refusal on this arc's beats; if it
    declined for any reason other than "the arc is finished", the answer
    is an impromptu scene, never the loss of a story in progress.
    """
    service, repo, planner = _service()
    character = _character()
    live = await service.start_new_arc(character, today=TODAY)
    calls_before = planner.calls

    assert await service.force_open_season(character, today=TODAY) is None

    assert planner.calls == calls_before
    reloaded = await repo.get(live.id)
    assert reloaded is not None
    assert reloaded.status not in (ARC_ABANDONED, ARC_COMPLETED)


@pytest.mark.asyncio
async def test_force_open_completes_a_stale_arc_before_opening_the_next(
) -> None:
    """An arc whose beats are all terminal is over, whatever its status."""
    service, repo, _ = _service()
    character = _character()
    stale = await service.start_new_arc(character, today=TODAY)
    for beat in stale.beats:
        await service.realize_beat(beat_id=beat.id, event_id=None)

    arc = await service.force_open_season(character, today=TODAY)

    assert arc is not None and arc.id != stale.id
    assert (await repo.get(stale.id)).status == ARC_COMPLETED


# ── series: progress must move, the planner must not fire ────────────


@pytest.mark.asyncio
async def test_force_open_starts_the_first_series_member() -> None:
    service, _, series, planner, character = await _series_service()

    arc = await service.force_open_season(character, today=TODAY)

    progress = await series.get_progress(character.id, "series-a")
    assert arc is not None
    assert arc.source_template_id == "book-one"
    assert planner.calls == 0
    assert progress is not None
    assert progress.current_index == 0
    assert progress.last_arc_id == arc.id


@pytest.mark.asyncio
async def test_force_open_advances_series_progress_to_the_next_book() -> None:
    """The reason ``start_new_arc`` could not be reused for this layer.

    ``start_new_arc`` does not know what a series is: calling it here
    would LLM-plan a season straight over the author's next book and
    leave ``CharacterSeriesProgress`` pointing at the one before it.
    """
    decider = _RecordingDecider(should_start=False)
    service, arcs, series, planner, character = await _series_service(
        decider=decider,
    )
    first = await service.force_open_season(character, today=TODAY)
    assert first is not None
    await arcs.save(first.with_status(ARC_COMPLETED))

    second = await service.force_open_season(
        character, today=TODAY + timedelta(days=8),
    )

    progress = await series.get_progress(character.id, "series-a")
    assert second is not None
    assert second.source_template_id == "book-two"
    assert planner.calls == 0
    # timing-only in series mode, so it is not even asked
    assert decider.contexts == []
    assert progress is not None
    assert progress.status == SERIES_STATUS_ACTIVE
    assert progress.current_index == 1
    assert progress.last_arc_id == second.id


@pytest.mark.asyncio
async def test_force_open_returns_none_when_the_series_has_concluded() -> None:
    """The waterfall's hand-off point to layer 3 (§3.1)."""
    service, arcs, series, planner, character = await _series_service()
    for _ in range(2):
        arc = await service.force_open_season(character, today=TODAY)
        assert arc is not None
        await arcs.save(arc.with_status(ARC_COMPLETED))

    assert await service.force_open_season(character, today=TODAY) is None

    progress = await series.get_progress(character.id, "series-a")
    assert planner.calls == 0
    assert progress is not None
    assert progress.status == SERIES_STATUS_CONCLUDED


# ── characterization: the scheduled paths keep their gate ────────────


@pytest.mark.asyncio
async def test_proactive_path_still_defers_to_the_decider() -> None:
    """``ensure_active_arc`` defaults (proactive) — the gate still holds."""
    decider = _RecordingDecider(should_start=False)
    service, _, planner = _service(decider=decider)
    character = _character()
    first = await service.start_new_arc(character, today=TODAY)
    for beat in first.beats:
        await service.realize_beat(beat_id=beat.id, event_id=None)
    calls_before = planner.calls

    assert await service.ensure_active_arc(
        character, today=TODAY, auto_start=True,
    ) is None

    assert len(decider.contexts) == 1
    assert planner.calls == calls_before


@pytest.mark.asyncio
async def test_chat_path_still_never_reaches_the_decider() -> None:
    """``open_new_season=False`` (chat prompt assembly, runtime init)."""
    decider = _RecordingDecider(should_start=True, hint="開下一季")
    service, _, planner = _service(decider=decider)
    character = _character()
    first = await service.start_new_arc(character, today=TODAY)
    for beat in first.beats:
        await service.realize_beat(beat_id=beat.id, event_id=None)
    calls_before = planner.calls

    assert await service.ensure_active_arc(
        character, today=TODAY, auto_start=True, open_new_season=False,
    ) is None

    assert decider.contexts == []
    assert planner.calls == calls_before


@pytest.mark.asyncio
async def test_read_only_path_still_plans_nothing() -> None:
    """``auto_start=False`` (schedule, life context)."""
    decider = _RecordingDecider(should_start=True, hint="開下一季")
    service, _, planner = _service(decider=decider)
    character = _character()

    assert await service.ensure_active_arc(
        character, today=TODAY, auto_start=False,
    ) is None

    assert decider.contexts == []
    assert planner.calls == 0


@pytest.mark.asyncio
async def test_scheduled_series_hand_off_still_asks_the_decider() -> None:
    decider = _RecordingDecider(should_start=True)
    service, arcs, series, planner, character = await _series_service(
        decider=decider,
    )
    first = await service.ensure_active_arc(character, today=TODAY)
    assert first is not None
    await arcs.save(first.with_status(ARC_COMPLETED))

    second = await service.ensure_active_arc(
        character, today=TODAY + timedelta(days=8),
    )

    assert second is not None
    assert second.source_template_id == "book-two"
    assert len(decider.contexts) == 1
    assert decider.contexts[0].series_id == "series-a"
    assert decider.contexts[0].next_template_title == "第二本"
    assert planner.calls == 0
    progress = await series.get_progress(character.id, "series-a")
    assert progress is not None and progress.current_index == 1
