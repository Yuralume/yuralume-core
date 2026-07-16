"""LLMSchedulePlanner renders the calendar_context block in its prompt.

We capture the prompt string sent to the model and assert the block
lands in the right place (before the dialogue / arc sections so the
"today is 春節" framing contextualises the rest).
"""

from __future__ import annotations

from datetime import date, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.schedule.llm_planner import LLMSchedulePlanner

UTC = timezone.utc


class _CapturingModel:
    def __init__(self) -> None:
        self.last_prompt = ""

    async def generate(self, prompt: str) -> str:
        self.last_prompt = prompt
        return "[]"  # empty schedule — we only care about the prompt

    def generate_stream(self, prompt: str):  # pragma: no cover - unused
        async def _empty():
            if False:
                yield ""
        return _empty()


def _character() -> Character:
    return Character.create(
        name="Aki",
        summary="大學生",
        personality=["內向"],
        interests=["咖啡"],
        speaking_style="溫柔",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


@pytest.mark.asyncio
async def test_calendar_context_appears_in_prompt() -> None:
    model = _CapturingModel()
    planner = LLMSchedulePlanner(model=model)
    await planner.plan_day(
        character=_character(),
        date_=date(2026, 1, 1),
        local_tz=UTC,
        calendar_context="今天是 2026-01-01（星期四）。國定假日「開國紀念日」。",
    )
    assert "今日真實世界行事曆" in model.last_prompt
    assert "開國紀念日" in model.last_prompt


@pytest.mark.asyncio
async def test_calendar_block_omitted_when_empty() -> None:
    model = _CapturingModel()
    planner = LLMSchedulePlanner(model=model)
    await planner.plan_day(
        character=_character(),
        date_=date(2026, 5, 19),
        local_tz=UTC,
        calendar_context="",
    )
    assert "今日真實世界行事曆" not in model.last_prompt


@pytest.mark.asyncio
async def test_calendar_block_precedes_arc_block() -> None:
    """Ordering matters: today's calendar facts should land before
    arc / dialogue framing so the model reads "what kind of day" before
    "what scene to play"."""
    model = _CapturingModel()
    planner = LLMSchedulePlanner(model=model)
    await planner.plan_day(
        character=_character(),
        date_=date(2026, 1, 1),
        local_tz=UTC,
        calendar_context="calendar-token-X",
        recent_dialogue_summary="dialogue-token-Y",
    )
    cal_idx = model.last_prompt.find("calendar-token-X")
    dlg_idx = model.last_prompt.find("dialogue-token-Y")
    assert cal_idx != -1 and dlg_idx != -1
    assert cal_idx < dlg_idx
