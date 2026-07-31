"""LLMSchedulePlanner weather discipline in the planning prompt.

Two complementary rules live in the prompt (SCHEDULE_WEATHER_DRIFT_PLAN
Workstream A):

* **No weather facts supplied** (the future-day pre-plan path) — the
  planner must not invent or hint at concrete weather. Without this the
  "描述要具體、有畫面感" directive happily produces rainy afternoons for a
  day nobody has a forecast for.
* **Weather facts supplied** — they're a *planning-time* snapshot, so the
  freshness caveat rides along with the fact block and the principles tell
  the model not to bake the weather state into the description.
"""

from __future__ import annotations

from datetime import date, timezone

import pytest

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.schedule.llm_planner import LLMSchedulePlanner

UTC = timezone.utc

_WEATHER_TEXT = "台北目前天氣：晴朗，氣溫 28°C，降雨機率 10%"


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


async def _prompt_for(weather_context: str) -> str:
    model = _CapturingModel()
    planner = LLMSchedulePlanner(model=model)
    await planner.plan_day(
        character=_character(),
        date_=date(2026, 5, 19),
        local_tz=UTC,
        weather_context=weather_context,
    )
    return model.last_prompt


@pytest.mark.asyncio
async def test_no_weather_facts_forbids_concrete_weather_in_descriptions() -> None:
    prompt = await _prompt_for("")
    # No rendered fact block — the principles still *name* the section, so
    # match on the rendered header, not the bare phrase.
    assert "此刻真實世界天氣（" not in prompt
    assert "沒有提供「此刻真實世界天氣」事實" in prompt
    assert "不得提及或暗示具體天氣" in prompt
    assert "天氣中性" in prompt


@pytest.mark.asyncio
async def test_weather_facts_carry_planning_time_freshness_caveat() -> None:
    prompt = await _prompt_for(_WEATHER_TEXT)
    assert "此刻真實世界天氣（此為規劃當下的即時事實，當天天氣可能變化）" in prompt
    assert "晴朗" in prompt
    assert "不要把天氣狀態寫死進 description" in prompt


@pytest.mark.asyncio
async def test_freshness_caveat_only_rides_with_the_fact_block() -> None:
    """The caveat lives in exactly one place — the rendered fact block —
    so an empty ``weather_context`` never ships a dangling reference to a
    weather section that isn't there."""
    empty_prompt = await _prompt_for("")
    weather_prompt = await _prompt_for(_WEATHER_TEXT)
    caveat = "此為規劃當下的即時事實，當天天氣可能變化"
    assert caveat not in empty_prompt
    assert weather_prompt.count(caveat) == 1


@pytest.mark.asyncio
async def test_weather_discipline_lives_in_the_planning_principles() -> None:
    """Both rules are stated up in 規劃原則 (before 角色資料) so the model
    reads them alongside the "描述要具體、有畫面感" directive they temper."""
    prompt = await _prompt_for(_WEATHER_TEXT)
    principles_idx = prompt.find("規劃原則：")
    character_idx = prompt.find("角色資料：")
    no_facts_idx = prompt.find("不得提及或暗示具體天氣")
    has_facts_idx = prompt.find("不要把天氣狀態寫死進 description")
    assert principles_idx != -1 and character_idx != -1
    assert principles_idx < no_facts_idx < character_idx
    assert principles_idx < has_facts_idx < character_idx
