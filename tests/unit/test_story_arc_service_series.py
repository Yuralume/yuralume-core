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
    CharacterSeriesProgress,
)
from kokoro_link.domain.entities.arc_template import ArcTemplate, ArcTemplateBeat
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import ARC_COMPLETED, StoryArc, StoryArcBeat
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


class _RecordingPlanner(StoryArcPlannerPort):
    def __init__(self) -> None:
        self.calls = 0

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
        arc = StoryArc.create(
            character_id=character.id,
            title="LLM arc",
            premise="planner should not be used for series",
            theme="custom",
            start_date=start_date,
            end_date=start_date + timedelta(days=duration_days),
        )
        beat = StoryArcBeat.create(
            arc_id=arc.id,
            sequence=0,
            scheduled_date=start_date,
            title="LLM beat",
            summary="LLM-generated beat",
        )
        return arc.with_beats([beat])


class _RecordingDecider(StoryArcSeasonDeciderPort):
    def __init__(self, should_start: bool) -> None:
        self.should_start = should_start
        self.contexts: list[StoryArcSeasonContext] = []

    async def decide(
        self,
        context: StoryArcSeasonContext,
    ) -> StoryArcSeasonDecision:
        self.contexts.append(context)
        return StoryArcSeasonDecision(
            should_start=self.should_start,
            reason="test",
            hint="ignored in series mode",
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
                sequence=0,
                day_offset=0,
                title=f"{title} opening",
                summary="第一場戲。",
            ),
        ],
    )


def _character() -> Character:
    return Character.create(
        name="Aki",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
        user_id="user-a",
        arc_series_id="series-a",
    )


async def _series_fixture(
    *,
    decider: _RecordingDecider | None = None,
) -> tuple[
    StoryArcService,
    InMemoryStoryArcRepository,
    InMemoryArcSeriesRepository,
    _RecordingPlanner,
    Character,
]:
    story_repo = InMemoryStoryArcRepository()
    series_repo = InMemoryArcSeriesRepository()
    template_repo = InMemoryArcTemplateRepository()
    planner = _RecordingPlanner()
    character = _character()
    await template_repo.save_for_user(
        _template("book-one", "第一本"),
        user_id="user-a",
    )
    await template_repo.save_for_user(
        _template("book-two", "第二本"),
        user_id="user-a",
    )
    await series_repo.save_for_user(
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
        repository=story_repo,
        planner=planner,
        template_repository=template_repo,
        series_repository=series_repo,
        season_decider=decider,
    )
    return service, story_repo, series_repo, planner, character


@pytest.mark.asyncio
async def test_series_bound_character_starts_first_member_without_planner() -> None:
    service, _, series_repo, planner, character = await _series_fixture()

    arc = await service.ensure_active_arc(
        character,
        today=date(2026, 6, 1),
    )

    progress = await series_repo.get_progress(character.id, "series-a")
    assert arc is not None
    assert arc.title == "第一本"
    assert arc.source_template_id == "book-one"
    assert planner.calls == 0
    assert progress is not None
    assert progress.status == SERIES_STATUS_ACTIVE
    assert progress.current_index == 0
    assert progress.last_arc_id == arc.id


@pytest.mark.asyncio
async def test_completed_series_member_decider_starts_next_member() -> None:
    decider = _RecordingDecider(should_start=True)
    service, story_repo, series_repo, planner, character = await _series_fixture(
        decider=decider,
    )
    first = await service.ensure_active_arc(character, today=date(2026, 6, 1))
    assert first is not None
    await story_repo.save(first.with_status(ARC_COMPLETED))

    second = await service.ensure_active_arc(
        character,
        today=date(2026, 6, 9),
    )

    progress = await series_repo.get_progress(character.id, "series-a")
    assert second is not None
    assert second.title == "第二本"
    assert second.source_template_id == "book-two"
    assert planner.calls == 0
    assert len(decider.contexts) == 1
    assert decider.contexts[0].series_id == "series-a"
    assert decider.contexts[0].series_title == "連載篇"
    assert decider.contexts[0].next_template_id == "book-two"
    assert decider.contexts[0].next_template_title == "第二本"
    assert progress is not None
    assert progress.current_index == 1
    assert progress.last_arc_id == second.id


@pytest.mark.asyncio
async def test_completed_last_series_member_concludes_without_planner() -> None:
    decider = _RecordingDecider(should_start=True)
    service, story_repo, series_repo, planner, character = await _series_fixture(
        decider=decider,
    )
    first = await service.ensure_active_arc(character, today=date(2026, 6, 1))
    assert first is not None
    await story_repo.save(first.with_status(ARC_COMPLETED))
    second = await service.ensure_active_arc(character, today=date(2026, 6, 9))
    assert second is not None
    await story_repo.save(second.with_status(ARC_COMPLETED))

    result = await service.ensure_active_arc(
        character,
        today=date(2026, 6, 17),
    )

    progress = await series_repo.get_progress(character.id, "series-a")
    assert result is None
    assert planner.calls == 0
    assert progress is not None
    assert progress.status == SERIES_STATUS_CONCLUDED


@pytest.mark.asyncio
async def test_series_decider_can_defer_next_member_without_advancing() -> None:
    decider = _RecordingDecider(should_start=False)
    service, story_repo, series_repo, planner, character = await _series_fixture(
        decider=decider,
    )
    first = await service.ensure_active_arc(character, today=date(2026, 6, 1))
    assert first is not None
    await story_repo.save(first.with_status(ARC_COMPLETED))

    result = await service.ensure_active_arc(
        character,
        today=date(2026, 6, 9),
    )

    progress = await series_repo.get_progress(character.id, "series-a")
    assert result is None
    assert planner.calls == 0
    assert progress is not None
    assert progress.status == SERIES_STATUS_ACTIVE
    assert progress.current_index == 0
    assert progress.last_arc_id == first.id


@pytest.mark.asyncio
async def test_missing_bound_series_falls_back_to_llm_planner() -> None:
    story_repo = InMemoryStoryArcRepository()
    series_repo = InMemoryArcSeriesRepository()
    template_repo = InMemoryArcTemplateRepository()
    planner = _RecordingPlanner()
    character = _character()
    service = StoryArcService(
        repository=story_repo,
        planner=planner,
        template_repository=template_repo,
        series_repository=series_repo,
    )

    arc = await service.ensure_active_arc(
        character,
        today=date(2026, 6, 1),
    )

    assert arc is not None
    assert arc.title == "LLM arc"
    assert planner.calls == 1


@pytest.mark.asyncio
async def test_series_progress_index_beyond_members_concludes_fail_soft() -> None:
    service, _, series_repo, planner, character = await _series_fixture()
    await series_repo.save_progress(
        CharacterSeriesProgress(
            character_id=character.id,
            series_id="series-a",
            current_index=99,
        )
    )

    result = await service.ensure_active_arc(
        character,
        today=date(2026, 6, 1),
    )

    progress = await series_repo.get_progress(character.id, "series-a")
    assert result is None
    assert planner.calls == 0
    assert progress is not None
    assert progress.status == SERIES_STATUS_CONCLUDED
