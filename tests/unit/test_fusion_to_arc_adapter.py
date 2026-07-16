from __future__ import annotations

import json

import pytest

from kokoro_link.contracts.fusion_to_arc import FusionToArcContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.fusion_story import FusionStory
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.story.fusion_to_arc_adapter import (
    LLMFusionToArcAdapter,
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


def _character(character_id: str, name: str) -> Character:
    character = Character.create(
        name=name,
        summary=f"{name} keeps promises but avoids direct confession.",
        personality=["careful", "warm"],
        interests=["late-night walks"],
        speaking_style="soft and precise",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=55,
            fatigue=0,
            trust=55,
            energy=90,
        ),
        world_frame="modern",
    )
    object.__setattr__(character, "id", character_id)
    return character


def _ready_story() -> FusionStory:
    return FusionStory.create_pending(
        id="fusion-1",
        character_ids=["c-a", "c-b"],
        prompt="Two friends revisit a promise after drifting apart.",
    ).with_status("ready").with_full_text(
        "Aki and Ren meet at the closed observatory, argue about the "
        "promise they buried, then decide to reopen it slowly."
    )


def _draft_json() -> str:
    return json.dumps(
        {
            "id": "observatory_promise",
            "title": "Observatory Promise",
            "premise": (
                "A quiet multi-day arc where old friends test whether a "
                "buried promise can become present-tense trust."
            ),
            "theme": "friendship",
            "tone": "daily",
            "duration_days": 7,
            "world_frames": ["modern"],
            "required_traits": [],
            "beats": [
                {
                    "sequence": 0,
                    "day_offset": 0,
                    "title": "Locked Gate",
                    "summary": (
                        "Aki finds the old observatory gate locked and must "
                        "decide whether to ask Ren why they stopped coming."
                    ),
                    "tension": "setup",
                    "scene_type": "encounter",
                    "location": "old observatory",
                    "scene_characters": ["Aki", "Ren"],
                    "dramatic_question": "Will either of them name the promise?",
                    "required": True,
                },
                {
                    "sequence": 1,
                    "day_offset": 4,
                    "title": "Returned Key",
                    "summary": (
                        "Ren returns with the key and makes a small apology "
                        "that gives Aki a reason to risk trust again."
                    ),
                    "tension": "resolution",
                    "scene_type": "resolution",
                    "location": "old observatory",
                    "scene_characters": ["Aki", "Ren"],
                    "dramatic_question": "Can the promise become a practice?",
                    "required": True,
                },
            ],
        },
        ensure_ascii=False,
    )


@pytest.mark.asyncio
async def test_adapter_returns_template_draft_from_ready_story_context() -> None:
    model = _ScriptedModel(_draft_json())
    adapter = LLMFusionToArcAdapter(model=model)

    draft = await adapter.adapt(
        FusionToArcContext(
            story=_ready_story(),
            characters=(
                _character("c-a", "Aki"),
                _character("c-b", "Ren"),
            ),
            instruction="Keep it intimate and slow.",
        )
    )

    assert draft is not None
    assert draft.id == "observatory_promise"
    assert draft.theme == "friendship"
    assert [beat.sequence for beat in draft.beats] == [0, 1]
    assert model.last_prompt is not None
    assert "semantic adaptation" in model.last_prompt
    assert "Keep it intimate and slow." in model.last_prompt
    assert "Aki" in model.last_prompt


@pytest.mark.asyncio
async def test_adapter_bad_json_returns_none() -> None:
    adapter = LLMFusionToArcAdapter(model=_ScriptedModel("not json"))

    draft = await adapter.adapt(
        FusionToArcContext(
            story=_ready_story(),
            characters=(_character("c-a", "Aki"),),
        )
    )

    assert draft is None
