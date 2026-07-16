from __future__ import annotations

from datetime import date

import pytest

from kokoro_link.contracts.story_arc import StoryBeatSceneContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import (
    StoryArc,
    StoryArcBeat,
    TENSION_RISING,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.companion import CharacterCompanion
from kokoro_link.infrastructure.story.llm_beat_scene_writer import (
    LLMStoryBeatSceneWriter,
    NullStoryBeatSceneWriter,
)


class _ScriptedModel:
    provider_id = "scripted"
    supports_vision = False

    def __init__(self, response: str) -> None:
        self.response = response
        self.prompts: list[str] = []

    async def generate(self, prompt: str, **kwargs):  # noqa: ANN003
        self.prompts.append(prompt)
        return self.response

    async def generate_stream(self, prompt: str, **kwargs):  # noqa: ANN003
        yield await self.generate(prompt, **kwargs)

    async def list_models(self) -> list[str]:
        return ["scripted"]


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="想成為演員的學生。",
        personality=[],
        interests=[],
        speaking_style="坦率",
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
                brief_profile="嚴厲但可靠",
                relationship_snippet="知道她每次上台前都會逃避",
            ),
        ),
    )


def _context(operator_primary_language: str = "zh-TW") -> StoryBeatSceneContext:
    character = _character()
    today = date(2026, 6, 1)
    arc = StoryArc.create(
        character_id=character.id,
        title="試鏡週",
        premise="她準備面對重要試鏡。",
        theme="ambition",
        tone="dramatic",
        start_date=today,
        end_date=today,
    )
    beat = StoryArcBeat.create(
        arc_id=arc.id,
        sequence=0,
        scheduled_date=today,
        title="最後提醒",
        summary="老師逼她承認自己想贏。",
        tension=TENSION_RISING,
        scene_characters=("指導老師",),
        location="排練室門口",
        dramatic_question="她敢不敢承認自己想贏？",
        play_attempt_count=2,
        last_play_attempt_source="chat_scene_directive",
        last_play_attempt_result="prompted",
        last_play_push_intensity="scene_directive",
    )
    return StoryBeatSceneContext(
        character=character,
        arc=arc.with_beats([beat]),
        beat=beat,
        today=today,
        operator_primary_language=operator_primary_language,
        user_involvement_policy="使用者不在場；請讓 NPC 完成。",
    )


@pytest.mark.asyncio
async def test_writer_parses_scene_json_and_renders_prompt_facts() -> None:
    model = _ScriptedModel(
        """
        {
          "narrative": "我站在排練室門口，指導老師只問我還要逃到什麼時候。",
          "emotional_tone": "tense",
          "cast_strategy": "npc_dialogue",
          "participation_note": "user not required"
        }
        """,
    )
    writer = LLMStoryBeatSceneWriter(model=model)

    draft = await writer.write_scene(_context())

    assert draft.narrative.startswith("我站在排練室門口")
    assert draft.emotional_tone == "tense"
    assert draft.cast_strategy == "npc_dialogue"
    assert draft.participation_note == "user not required"
    prompt = model.prompts[0]
    assert "排練室門口" in prompt
    assert "指導老師" in prompt
    assert "嚴厲但可靠" in prompt
    assert "已嘗試帶出次數：2" in prompt
    assert "使用者不在場" in prompt


@pytest.mark.asyncio
async def test_writer_falls_back_on_malformed_json() -> None:
    writer = LLMStoryBeatSceneWriter(model=_ScriptedModel("not json"))

    draft = await writer.write_scene(_context())

    assert draft.narrative
    assert draft.cast_strategy in {"npc_dialogue", "inner_monologue"}


@pytest.mark.asyncio
async def test_null_writer_uses_scene_characters_without_user_dependency() -> None:
    draft = await NullStoryBeatSceneWriter().write_scene(_context())

    assert "指導老師" in draft.narrative
    assert "user dependency" in draft.participation_note


@pytest.mark.asyncio
async def test_null_writer_narrative_localized_for_en_operator() -> None:
    """The deterministic fallback narrative must respect the operator's
    content language — an en-US operator must not get zh-TW prose."""
    draft = await NullStoryBeatSceneWriter().write_scene(_context("en-US"))

    # Beat title (proper noun) is preserved verbatim; the scaffolding
    # sentence around it must be English, not Chinese.
    assert "最後提醒" in draft.narrative
    assert "把" not in draft.narrative
    assert "說開" not in draft.narrative
    assert draft.cast_strategy == "npc_dialogue"


@pytest.mark.asyncio
async def test_null_writer_narrative_localized_for_ja_operator() -> None:
    draft = await NullStoryBeatSceneWriter().write_scene(_context("ja-JP"))

    assert "最後提醒" in draft.narrative
    # zh-TW-only tokens must not leak into a ja narrative.
    assert "把" not in draft.narrative
    assert "說開" not in draft.narrative


@pytest.mark.asyncio
async def test_null_writer_inner_monologue_localized_for_en_operator() -> None:
    """No scene_characters → inner-monologue branch, also localized."""
    ctx = _context("en-US")
    beat_no_cast = ctx.beat.__class__.create(
        arc_id=ctx.beat.arc_id,
        sequence=0,
        scheduled_date=ctx.today,
        title="Solo Rehearsal",
        summary="She practices alone.",
        tension=TENSION_RISING,
        scene_characters=(),
        location="",
    )
    ctx = StoryBeatSceneContext(
        character=ctx.character,
        arc=ctx.arc,
        beat=beat_no_cast,
        today=ctx.today,
        operator_primary_language="en-US",
    )

    draft = await NullStoryBeatSceneWriter().write_scene(ctx)

    assert draft.cast_strategy == "inner_monologue"
    assert "Solo Rehearsal" in draft.narrative
    assert "獨自面對" not in draft.narrative
