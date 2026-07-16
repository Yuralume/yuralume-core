from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from kokoro_link.contracts.story_arc import StoryBeatRecheckContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.story.llm_beat_rechecker import (
    LLMStoryBeatRechecker,
    NullStoryBeatRechecker,
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
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )


def _context() -> StoryBeatRecheckContext:
    character = _character()
    today = date(2026, 6, 1)
    arc = StoryArc.create(
        character_id=character.id,
        title="試鏡週",
        premise="她要面對試鏡。",
        theme="ambition",
        start_date=today,
        end_date=today,
    )
    beat = StoryArcBeat.create(
        arc_id=arc.id,
        sequence=0,
        scheduled_date=today,
        title="承認想贏",
        summary="她終於承認自己不是只想試試看，而是真的想贏。",
        play_attempt_count=2,
        last_play_attempt_at=datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc),
        last_play_attempt_source="chat_scene_directive",
        last_play_attempt_result="prompted",
        last_play_push_intensity="scene_directive",
    )
    return StoryBeatRecheckContext(
        character=character,
        arc=arc.with_beats([beat]),
        beat=beat,
        today=today,
        recent_dialogue_summary="她剛才說：我其實很想贏。",
    )


@pytest.mark.asyncio
async def test_rechecker_parses_mark_realized_and_renders_attempts() -> None:
    model = _ScriptedModel(
        """
        {
          "action": "mark_realized",
          "reason": "對話已說出核心轉折",
          "days": null,
          "narrative": "我終於承認自己是真的想贏。"
        }
        """,
    )
    rechecker = LLMStoryBeatRechecker(model=model)

    decision = await rechecker.recheck(_context())

    assert decision.action == "mark_realized"
    assert decision.narrative == "我終於承認自己是真的想贏。"
    prompt = model.prompts[0]
    assert "play_attempt_count: 2" in prompt
    assert "我其實很想贏" in prompt
    assert "不要因為 scheduled_date 到了就 mark_realized" in prompt


@pytest.mark.asyncio
async def test_rechecker_falls_back_when_mark_realized_lacks_narrative() -> None:
    rechecker = LLMStoryBeatRechecker(
        model=_ScriptedModel('{"action":"mark_realized","reason":"too thin"}'),
    )

    decision = await rechecker.recheck(_context())

    assert decision.action == "keep_pending"


@pytest.mark.asyncio
async def test_null_rechecker_keeps_pending() -> None:
    decision = await NullStoryBeatRechecker().recheck(_context())

    assert decision.action == "keep_pending"
