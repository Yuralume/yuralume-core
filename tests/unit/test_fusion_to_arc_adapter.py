from __future__ import annotations

import json

import pytest

from kokoro_link.contracts.fusion_to_arc import (
    FUSION_OPERATOR_MODE_OBSERVER,
    FUSION_OPERATOR_MODE_UNCHANGED,
    FUSION_OPERATOR_MODE_WRITE_IN,
    FusionToArcContext,
)
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


def _positioned_draft_json(
    *pairs: tuple[str | None, str | None],
) -> str:
    """Two-beat draft whose beats carry the given (position, note).

    ``None`` for a field omits the key entirely, which is how a model
    that predates OP1-C answers.
    """
    payload = json.loads(_draft_json())
    for beat, (position, note) in zip(payload["beats"], pairs, strict=True):
        if position is not None:
            beat["operator_position"] = position
        if note is not None:
            beat["operator_note"] = note
    return json.dumps(payload, ensure_ascii=False)


async def _adapt(
    response: str,
    *,
    mode: str | None = None,
    relationship_lines: tuple[str, ...] = (),
) -> tuple[object, _ScriptedModel]:
    model = _ScriptedModel(response)
    adapter = LLMFusionToArcAdapter(model=model)
    kwargs = {} if mode is None else {"operator_mode": mode}
    draft = await adapter.adapt(
        FusionToArcContext(
            story=_ready_story(),
            characters=(_character("c-a", "Aki"), _character("c-b", "Ren")),
            operator_relationship_lines=relationship_lines,
            **kwargs,
        )
    )
    return draft, model


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


# ---------- OP1-C: the creator's three-way choice ----------------------


@pytest.mark.asyncio
async def test_write_in_mode_keeps_the_positions_the_model_judged() -> None:
    """`write_in` is the one mode where the answer is per beat, so the
    adapter must not overwrite what the model decided."""
    draft, model = await _adapt(
        _positioned_draft_json(
            ("central", "她要在這裡把話說完"),
            ("absent", ""),
        ),
        mode=FUSION_OPERATOR_MODE_WRITE_IN,
    )

    assert draft is not None
    assert [b.operator_position for b in draft.beats] == ["central", "absent"]
    assert draft.beats[0].operator_note == "她要在這裡把話說完"
    assert draft.beats[1].operator_note is None
    assert "WRITE THE PLAYER IN" in (model.last_prompt or "")


@pytest.mark.asyncio
async def test_write_in_mode_survives_a_model_that_omits_the_fields() -> None:
    """An older model that never saw the schema still adapts; the beats
    read back unjudged rather than the call failing."""
    draft, _ = await _adapt(
        _draft_json(), mode=FUSION_OPERATOR_MODE_WRITE_IN,
    )

    assert draft is not None
    assert [b.operator_position for b in draft.beats] == [None, None]


@pytest.mark.asyncio
async def test_write_in_mode_carries_the_player_relationship_facts() -> None:
    _, model = await _adapt(
        _draft_json(),
        mode=FUSION_OPERATOR_MODE_WRITE_IN,
        relationship_lines=("Aki：", "- 角色怎麼稱呼玩家：小羽"),
    )

    assert "- 角色怎麼稱呼玩家：小羽" in (model.last_prompt or "")


@pytest.mark.asyncio
async def test_no_relationship_facts_renders_no_heading() -> None:
    """An empty heading is a hole the model fills by inventing a
    relationship nobody agreed to."""
    _, model = await _adapt(_draft_json(), mode=FUSION_OPERATOR_MODE_WRITE_IN)

    assert "Who the player is to this cast" not in (model.last_prompt or "")


@pytest.mark.asyncio
async def test_observer_mode_puts_the_player_at_every_beat_as_a_witness() -> None:
    draft, model = await _adapt(
        _positioned_draft_json(
            ("central", "你就站在她面前"),
            (None, None),
        ),
        mode=FUSION_OPERATOR_MODE_OBSERVER,
    )

    assert draft is not None
    # "I watch this story" means the player is a witness to all of it —
    # including the beat the model tried to make about them.
    assert [b.operator_position for b in draft.beats] == ["present", "present"]
    # The witness note is prose the writer prompts will use; it survives.
    assert draft.beats[0].operator_note == "你就站在她面前"
    assert "THE PLAYER WATCHES THIS STORY" in (model.last_prompt or "")


@pytest.mark.asyncio
async def test_unchanged_mode_forces_every_beat_absent_and_drops_notes() -> None:
    """紅線 5 — 「純角色戲保持原樣」 really is unchanged: a model that
    tries to hand the player a scene anyway cannot."""
    draft, _ = await _adapt(
        _positioned_draft_json(
            ("central", "她要向你坦白"),
            ("present", "你在旁邊看著"),
        ),
        mode=FUSION_OPERATOR_MODE_UNCHANGED,
    )

    assert draft is not None
    assert [b.operator_position for b in draft.beats] == ["absent", "absent"]
    assert [b.operator_note for b in draft.beats] == [None, None]


@pytest.mark.asyncio
async def test_unchanged_mode_prompt_never_licenses_a_rewrite() -> None:
    """紅線 5, negative — the instructions that permit re-casting scenes
    around the player are physically absent from the prompt, not merely
    overridden later in it. The player's name never reaches the model
    either, even when the caller supplies it."""
    _, model = await _adapt(
        _draft_json(),
        mode=FUSION_OPERATOR_MODE_UNCHANGED,
        relationship_lines=("Aki：", "- 角色怎麼稱呼玩家：小羽"),
    )
    prompt = model.last_prompt or ""

    assert "KEEP IT A PURE CHARACTER STORY" in prompt
    assert "WRITE THE PLAYER IN" not in prompt
    assert "THE PLAYER WATCHES THIS STORY" not in prompt
    assert "re-imagine" not in prompt
    assert "Re-casting" not in prompt
    assert "小羽" not in prompt
    assert "Who the player is to this cast" not in prompt
    # The source-preserving instruction that the mode exists for.
    assert "Adapt the source as it was written" in prompt


@pytest.mark.asyncio
async def test_unknown_mode_falls_back_to_the_conservative_branch() -> None:
    """The API rejects an unknown mode outright; this adapter is also a
    library seam, so it degrades instead of failing mid-generation — and
    the prompt it renders and the rule it applies agree on which mode
    that was."""
    draft, model = await _adapt(
        _positioned_draft_json(("central", "她要向你坦白"), ("present", "")),
        mode="spectator",
    )

    assert draft is not None
    assert [b.operator_position for b in draft.beats] == ["absent", "absent"]
    assert "KEEP IT A PURE CHARACTER STORY" in (model.last_prompt or "")


@pytest.mark.asyncio
async def test_unstated_mode_leaves_the_creators_story_alone() -> None:
    """ARC_PLAYER_POSITION_PLAN §3.2 — a caller that never chose (an
    omitted field, a pre-OP1-C client) gets the mode that does not touch
    the prose, not a rewrite nobody asked for."""
    draft, model = await _adapt(
        _positioned_draft_json(("central", "她要向你坦白"), (None, None)),
    )

    assert draft is not None
    assert [b.operator_position for b in draft.beats] == ["absent", "absent"]
    assert "KEEP IT A PURE CHARACTER STORY" in (model.last_prompt or "")
