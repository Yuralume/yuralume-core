"""LLMSchedulePlanner surfaces recurring_patterns as a planner block.

HUMANIZATION_ROADMAP §3.3 — the planner LLM sees observed recurrences
as a fact-layer block ("近幾週觀察到的生活節奏") so it can decide
whether to continue the rhythm or break it. We assert here that the
block lands, the descriptions are quoted verbatim, and the absence of
patterns yields no block at all (so old planner behaviour stays
unchanged for new characters).
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from kokoro_link.domain.entities.behavioral_pattern import (
    KIND_RECURRING_ACTIVITY,
    KIND_TIME_PREFERENCE,
    BehavioralPattern,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.schedule.llm_planner import LLMSchedulePlanner

UTC = timezone.utc


class _CapturingModel:
    def __init__(self) -> None:
        self.last_prompt = ""

    async def generate(self, prompt: str) -> str:
        self.last_prompt = prompt
        return "[]"

    def generate_stream(self, prompt: str):  # pragma: no cover
        async def _empty():
            if False:
                yield ""
        return _empty()


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="平日忙碌的工程師",
        personality=["內向"],
        interests=["咖啡"],
        speaking_style="安靜",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _pattern(description: str, kind: str = KIND_RECURRING_ACTIVITY) -> BehavioralPattern:
    return BehavioralPattern.new(
        character_id="char-A",
        kind=kind,
        description=description,
        observed_count=4,
        salience=0.7,
        last_observed_at=datetime(2026, 5, 21, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_planner_surfaces_recurring_patterns_block() -> None:
    model = _CapturingModel()
    planner = LLMSchedulePlanner(model=model)

    await planner.plan_day(
        character=_character(),
        date_=date(2026, 5, 22),
        local_tz=UTC,
        recurring_patterns=(
            _pattern("星期一早晨常做「study」"),
            _pattern(
                "清晨是這位角色最活躍的時段之一",
                kind=KIND_TIME_PREFERENCE,
            ),
        ),
    )

    assert "近幾週觀察到的生活節奏" in model.last_prompt
    assert "星期一早晨常做「study」" in model.last_prompt
    assert "清晨是這位角色最活躍的時段之一" in model.last_prompt
    # observed_count should not leak — LLM must not see raw numbers.
    assert "observed_count" not in model.last_prompt


@pytest.mark.asyncio
async def test_planner_omits_block_without_patterns() -> None:
    model = _CapturingModel()
    planner = LLMSchedulePlanner(model=model)

    await planner.plan_day(
        character=_character(),
        date_=date(2026, 5, 22),
        local_tz=UTC,
    )

    assert "近幾週觀察到的生活節奏" not in model.last_prompt
