from __future__ import annotations

import pytest

from kokoro_link.application.services.arc_series_continuation_draft_service import (
    ArcSeriesContinuationDraftService,
)
from kokoro_link.application.services.arc_series_service import ArcSeriesValidationError
from kokoro_link.application.services.arc_template_intake_service import (
    BeatDraft,
    TemplateDraft,
)
from kokoro_link.domain.entities.arc_series import (
    ArcSeries,
    CharacterSeriesProgress,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_arc_series import (
    InMemoryArcSeriesRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


class _RecordingAdapter:
    def __init__(self) -> None:
        self.context = None

    async def draft(self, context):
        self.context = context
        return TemplateDraft(
            id="next_season",
            title="Next Season",
            premise="A reviewable next-season draft.",
            theme="growth",
            beats=(
                BeatDraft(
                    sequence=0,
                    day_offset=0,
                    title="New Door",
                    summary="A concrete opening scene.",
                ),
            ),
        )


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="A singer.",
        personality=[],
        interests=[],
        speaking_style="natural",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
        user_id="alice",
    )


@pytest.mark.asyncio
async def test_draft_next_season_requires_concluded_progress_and_returns_unsaved_draft() -> None:
    character_repo = InMemoryCharacterRepository()
    series_repo = InMemoryArcSeriesRepository()
    character = _character()
    await character_repo.save(character)
    await series_repo.save_for_user(
        ArcSeries.create(
            id="series-a",
            title="Series A",
            premise="A fixed story.",
            template_ids=["book-one", "book-two"],
            user_id="alice",
        ),
        user_id="alice",
    )
    await series_repo.save_progress(
        CharacterSeriesProgress.start(
            character_id=character.id,
            series_id="series-a",
        ).with_started_member(
            index=1,
            arc_id="arc-two",
        ).concluded(),
    )
    adapter = _RecordingAdapter()
    service = ArcSeriesContinuationDraftService(
        series_repository=series_repo,
        character_repository=character_repo,
        adapter=adapter,
    )

    draft = await service.draft_next_season(
        series_id="series-a",
        character_id=character.id,
        user_id="alice",
        instruction="Keep it quiet.",
    )

    assert draft is not None
    assert draft.id == "next_season"
    assert adapter.context is not None
    assert adapter.context.series.id == "series-a"
    assert adapter.context.progress.status == "concluded"
    assert adapter.context.instruction == "Keep it quiet."


@pytest.mark.asyncio
async def test_draft_next_season_rejects_active_progress() -> None:
    character_repo = InMemoryCharacterRepository()
    series_repo = InMemoryArcSeriesRepository()
    character = _character()
    await character_repo.save(character)
    await series_repo.save_for_user(
        ArcSeries.create(
            id="series-a",
            title="Series A",
            premise="A fixed story.",
            template_ids=["book-one", "book-two"],
            user_id="alice",
        ),
        user_id="alice",
    )
    await series_repo.save_progress(
        CharacterSeriesProgress.start(
            character_id=character.id,
            series_id="series-a",
        ),
    )
    service = ArcSeriesContinuationDraftService(
        series_repository=series_repo,
        character_repository=character_repo,
        adapter=_RecordingAdapter(),
    )

    with pytest.raises(ArcSeriesValidationError, match="concluded"):
        await service.draft_next_season(
            series_id="series-a",
            character_id=character.id,
            user_id="alice",
        )
