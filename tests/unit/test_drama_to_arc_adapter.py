"""BD7 — the drama-path adaptation prompt and its mode enforcement."""

from __future__ import annotations

import json

import pytest

from kokoro_link.contracts.drama_to_arc import (
    DramaPathBeat,
    DramaToArcContext,
)
from kokoro_link.contracts.fusion_to_arc import (
    FUSION_OPERATOR_MODE_OBSERVER,
    FUSION_OPERATOR_MODE_UNCHANGED,
    FUSION_OPERATOR_MODE_WRITE_IN,
)
from kokoro_link.domain.entities.branching_drama import (
    OPERATOR_POSITION_CENTRAL,
    STATUS_READY,
    BranchingDrama,
    Exchange,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.story.drama_to_arc_adapter import (
    LLMDramaToArcAdapter,
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
        personality=["careful"],
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


def _drama() -> BranchingDrama:
    return (
        BranchingDrama.create_pending(
            id="drama-1",
            character_ids=["c-a", "c-b"],
            prompt="Find the signal under the observatory glass.",
            total_segments=3,
            operator_position=OPERATOR_POSITION_CENTRAL,
        )
        .with_title("Glass Signal")
        .with_status(STATUS_READY)
    )


def _path() -> tuple[DramaPathBeat, ...]:
    return (
        DramaPathBeat(
            sequence=0,
            depth=0,
            tone=None,
            title="Opening",
            summary="The glass roof hums.",
            narration="霧散開了。",
            exchanges=(
                Exchange(
                    player_input="我先開口。",
                    response="她抬起頭看向聲音的方向。",
                ),
            ),
        ),
        DramaPathBeat(
            sequence=1,
            depth=1,
            tone="dark",
            title="The Cut Wire",
            summary="She finds the wire has been cut on purpose.",
            narration="她把那截電線舉到燈下。",
        ),
    )


def _draft_json(**beat_overrides: object) -> str:
    beat = {
        "sequence": 0,
        "day_offset": 0,
        "title": "Locked Gate",
        "summary": (
            "Aki finds the observatory gate locked and must decide whether "
            "to ask why they stopped coming."
        ),
        "tension": "setup",
        "scene_type": "encounter",
        "required": True,
    }
    beat.update(beat_overrides)
    return json.dumps(
        {
            "id": "glass_signal_arc",
            "title": "Glass Signal",
            "premise": (
                "A quiet multi-day arc where a buried signal becomes a "
                "present-tense question between two people."
            ),
            "theme": "discovery",
            "tone": "dramatic",
            "duration_days": 7,
            "world_frames": ["modern"],
            "required_traits": [],
            "beats": [beat],
        },
        ensure_ascii=False,
    )


async def _adapt(response: str, *, mode: str | None = None):  # noqa: ANN201
    model = _ScriptedModel(response)
    adapter = LLMDramaToArcAdapter(model=model)
    kwargs = {} if mode is None else {"operator_mode": mode}
    draft = await adapter.adapt(
        DramaToArcContext(
            drama=_drama(),
            path=_path(),
            characters=(_character("c-a", "Aki"), _character("c-b", "Ren")),
            instruction="Keep it slow.",
            **kwargs,
        )
    )
    return draft, model


@pytest.mark.asyncio
async def test_prompt_carries_the_walked_path_and_says_what_it_is() -> None:
    draft, model = await _adapt(_draft_json(), mode=FUSION_OPERATOR_MODE_WRITE_IN)

    assert draft is not None
    assert draft.id == "glass_signal_arc"
    prompt = model.last_prompt or ""
    # The frozen fusion body is what defines the output schema…
    assert "semantic adaptation" in prompt
    assert "operator_position" in prompt
    # …and the code-side preamble is what says the source is a transcript.
    assert "分歧劇場" in prompt
    assert "玩家講的話是玩家的" in prompt
    # The line the player walked, outline joined to transcript.
    assert "The Cut Wire" in prompt
    assert "霧散開了。" in prompt
    # Everything the player said reaches the model as an exchange — there
    # is no beat-level input slot (FX3), and the preamble no longer
    # announces one.
    assert "我先開口。" in prompt
    assert "player_input" in prompt
    assert "Keep it slow." in prompt
    assert "Aki" in prompt


@pytest.mark.asyncio
async def test_bad_json_returns_none() -> None:
    draft, _ = await _adapt("not json")

    assert draft is None


@pytest.mark.asyncio
async def test_observer_forces_every_beat_to_present() -> None:
    draft, prompt_model = await _adapt(
        _draft_json(operator_position="central", operator_note="她只看著你"),
        mode=FUSION_OPERATOR_MODE_OBSERVER,
    )

    assert draft is not None
    assert [b.operator_position for b in draft.beats] == ["present"]
    # Observer keeps the note: the player is there, it is just not about them.
    assert draft.beats[0].operator_note == "她只看著你"
    assert "fusion/adapt_operator_observer" not in (prompt_model.last_prompt or "")


@pytest.mark.asyncio
async def test_unchanged_forces_absent_and_drops_the_player_note() -> None:
    """紅線 5 — a story the player declared they are not in cannot keep
    prose about the player's dramatic place."""
    draft, _ = await _adapt(
        _draft_json(operator_position="central", operator_note="她只看著你"),
        mode=FUSION_OPERATOR_MODE_UNCHANGED,
    )

    assert draft is not None
    assert [b.operator_position for b in draft.beats] == ["absent"]
    assert draft.beats[0].operator_note is None


@pytest.mark.asyncio
async def test_write_in_leaves_the_models_per_beat_judgement_alone() -> None:
    draft, _ = await _adapt(
        _draft_json(operator_position="central", operator_note="她只看著你"),
        mode=FUSION_OPERATOR_MODE_WRITE_IN,
    )

    assert draft is not None
    assert [b.operator_position for b in draft.beats] == ["central"]
    assert draft.beats[0].operator_note == "她只看著你"
