from __future__ import annotations

import json

import pytest

from kokoro_link.contracts.arc_series_continuation import (
    ArcSeriesContinuationContext,
)
from kokoro_link.domain.entities.arc_series import (
    ArcSeries,
    CharacterSeriesProgress,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.story.arc_series_continuation_adapter import (
    LLMArcSeriesContinuationDraftAdapter,
)


class _ScriptedModel:
    supports_vision = False

    def __init__(self, response: str) -> None:
        self.response = response
        self.last_prompt: str | None = None

    async def generate(self, prompt: str, **kwargs):  # noqa: ANN003
        self.last_prompt = prompt
        return self.response

    def generate_stream(self, prompt: str, **kwargs):  # noqa: ANN003
        async def _empty():
            if False:
                yield ""

        return _empty()


def _draft_json() -> str:
    return json.dumps(
        {
            "id": "next_season",
            "title": "Next Season",
            "premise": "A playable continuation seed.",
            "theme": "growth",
            "tone": "daily",
            "duration_days": 7,
            "world_frames": ["modern"],
            "required_traits": [],
            "beats": [
                {
                    "sequence": 0,
                    "day_offset": 0,
                    "title": "New Door",
                    "summary": "Mio finds a new door after the ending.",
                    "tension": "setup",
                    "scene_type": "encounter",
                    "required": True,
                },
            ],
        },
        ensure_ascii=False,
    )


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="A singer.",
        personality=["careful"],
        interests=["music"],
        speaking_style="natural",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
        world_frame="modern",
    )


@pytest.mark.asyncio
async def test_adapter_returns_template_draft_and_marks_prompt_authoring_only() -> None:
    model = _ScriptedModel(_draft_json())
    adapter = LLMArcSeriesContinuationDraftAdapter(model=model)
    context = ArcSeriesContinuationContext(
        character=_character(),
        series=ArcSeries.create(
            id="series-a",
            title="Series A",
            premise="A fixed story.",
            template_ids=["book-one", "book-two"],
        ),
        progress=CharacterSeriesProgress.start(
            character_id="char-a",
            series_id="series-a",
        ).concluded(),
        instruction="Keep the continuation quiet.",
    )

    draft = await adapter.draft(context)

    assert draft is not None
    assert draft.id == "next_season"
    assert draft.beats[0].title == "New Door"
    assert model.last_prompt is not None
    assert "authoring-only" in model.last_prompt
    assert "Do not modify runtime state" in model.last_prompt

