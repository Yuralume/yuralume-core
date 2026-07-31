"""Prompt-assembly and parser tests for the weather-drift judge.

Same shape as ``test_llm_activity_aftermath.py``: a stub chat model
returns canned payloads so we exercise
:mod:`kokoro_link.infrastructure.schedule.llm_weather_drift` without
touching a real backend.

Per the project's top directive we never assert on *which* activity the
model decided to rewrite — that judgement lives entirely in the prompt.
What is pinned here is the contract the adapter owes the application
service:

- the prompt carries the facts the judgement needs (persona, the current
  weather block, every window activity with its civil-time span);
- only ``rewrite`` verdicts come back, keyed by ids the caller supplied;
- every malformed / hallucinated shape degrades to "change nothing"
  instead of corrupting the day.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.schedule.llm_weather_drift import (
    LLMScheduleWeatherDriftJudge,
    NullScheduleWeatherDriftJudge,
    _build_prompt,
    _parse,
)


UTC = timezone.utc
TAIPEI = ZoneInfo("Asia/Taipei")

_WEATHER_BLOCK = (
    "台北市目前天氣（事實層；請自行從中推導角色該如何反應）：\n"
    "- 現在：晴朗｜氣溫 31°C\n"
    "- 今日溫度：高溫 33°C、低溫 26°C"
)


class _StubModel(ChatModelPort):
    def __init__(self, response: str = "") -> None:
        self.response = response
        self.prompts: list[str] = []

    async def generate(self, prompt: str, **kwargs: object) -> str:
        self.prompts.append(prompt)
        return self.response

    async def generate_stream(  # pragma: no cover - unused
        self, prompt: str,
    ) -> AsyncIterator[str]:
        yield self.response


class _RaisingModel(ChatModelPort):
    async def generate(self, prompt: str, **kwargs: object) -> str:
        raise RuntimeError("backend down")

    async def generate_stream(  # pragma: no cover - unused
        self, prompt: str,
    ) -> AsyncIterator[str]:
        yield ""


class _ActiveProvider:
    """Minimal ``ActiveLLMProviderPort`` stand-in so the fake-provider
    short circuit can be exercised without the container."""

    def __init__(self, model: ChatModelPort, *, fake: bool = False) -> None:
        self.model = model
        self.fake = fake

    async def resolve(self, feature_key=None, **kwargs: object):
        return self.model

    async def resolve_model_id(self, feature_key=None, **kwargs: object):
        return "scripted-model"

    async def is_fake(self, feature_key=None, **kwargs: object) -> bool:
        return self.fake


def _character() -> Character:
    return Character.create(
        name="Airi",
        summary="高中二年級的甜點愛好者",
        personality=["怕生"],
        interests=["甜點"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=20, trust=50, energy=80,
        ),
    )


def _walk() -> ScheduleActivity:
    """A weather-coupled block — 14:00–15:00 Taipei time.

    Built through the constructor rather than ``create`` so the id stays
    stable across the prompt and the canned model payloads.
    """
    return ScheduleActivity(
        id="act-walk",
        start_at=datetime(2026, 4, 18, 6, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 18, 7, 0, tzinfo=UTC),
        description="撐著傘在河堤邊躲雨慢走",
        category="leisure",
        location="河堤公園",
        busy_score=0.2,
    )


def _film() -> ScheduleActivity:
    """A weather-agnostic block — 15:30–17:30 Taipei time."""
    return ScheduleActivity(
        id="act-film",
        start_at=datetime(2026, 4, 18, 7, 30, tzinfo=UTC),
        end_at=datetime(2026, 4, 18, 9, 30, tzinfo=UTC),
        description="去電影院看新上映的片",
        category="leisure",
        location="信義威秀",
        busy_score=0.6,
    )


def _now() -> datetime:
    return datetime(2026, 4, 18, 6, 30, tzinfo=UTC)


_ALLOWED = frozenset({"act-walk", "act-film"})


class TestParse:
    def test_extracts_only_rewrites(self) -> None:
        raw = (
            '[{"activity_id": "act-walk", "action": "rewrite",'
            ' "description": "在河堤邊散步曬太陽", "location": "河堤公園",'
            ' "category": "leisure", "busy_score": 0.3},'
            ' {"activity_id": "act-film", "action": "keep"}]'
        )
        result = _parse(raw, allowed_ids=_ALLOWED)
        assert len(result) == 1
        rewrite = result[0]
        assert rewrite.activity_id == "act-walk"
        assert rewrite.description == "在河堤邊散步曬太陽"
        assert rewrite.location == "河堤公園"
        assert rewrite.category == "leisure"
        assert rewrite.busy_score == pytest.approx(0.3)

    def test_omitted_fields_stay_none(self) -> None:
        """Description-only rewrite must not blank the planner's
        location / category / busy_score."""
        raw = (
            '[{"activity_id": "act-walk", "action": "rewrite",'
            ' "description": "在河堤邊散步曬太陽"}]'
        )
        (rewrite,) = _parse(raw, allowed_ids=_ALLOWED)
        assert rewrite.location is None
        assert rewrite.category is None
        assert rewrite.busy_score is None

    def test_blank_string_fields_are_treated_as_omitted(self) -> None:
        raw = (
            '[{"activity_id": "act-walk", "action": "rewrite",'
            ' "description": "改成室內咖啡廳看書", "location": "  ",'
            ' "category": ""}]'
        )
        (rewrite,) = _parse(raw, allowed_ids=_ALLOWED)
        assert rewrite.location is None
        assert rewrite.category is None

    def test_strips_code_fence(self) -> None:
        raw = (
            "```json\n"
            '[{"activity_id": "act-walk", "action": "rewrite",'
            ' "description": "在河堤邊散步曬太陽"}]\n'
            "```"
        )
        result = _parse(raw, allowed_ids=_ALLOWED)
        assert len(result) == 1

    def test_blank_and_malformed_payloads_change_nothing(self) -> None:
        assert _parse("", allowed_ids=_ALLOWED) == ()
        assert _parse("   \n ", allowed_ids=_ALLOWED) == ()
        assert _parse("抱歉，我沒辦法判斷", allowed_ids=_ALLOWED) == ()
        assert _parse('{"activity_id": "act-walk"}', allowed_ids=_ALLOWED) == ()
        assert _parse('[{"activity_id": ', allowed_ids=_ALLOWED) == ()

    def test_drops_unknown_activity_id(self) -> None:
        """Hallucinated ids would otherwise rewrite blocks the caller
        deliberately excluded (encounters, confirmed promises)."""
        raw = (
            '[{"activity_id": "act-ghost", "action": "rewrite",'
            ' "description": "憑空冒出的段落"}]'
        )
        assert _parse(raw, allowed_ids=_ALLOWED) == ()

    def test_ignores_actions_other_than_rewrite(self) -> None:
        raw = (
            '[{"activity_id": "act-walk", "action": "delete"},'
            ' {"activity_id": "act-film", "action": "cancel",'
            ' "description": "取消"}]'
        )
        assert _parse(raw, allowed_ids=_ALLOWED) == ()

    def test_missing_action_is_not_a_rewrite(self) -> None:
        raw = '[{"activity_id": "act-walk", "description": "散步"}]'
        assert _parse(raw, allowed_ids=_ALLOWED) == ()

    def test_accepts_action_case_and_padding(self) -> None:
        raw = (
            '[{"activity_id": "act-walk", "action": " Rewrite ",'
            ' "description": "在河堤邊散步曬太陽"}]'
        )
        assert len(_parse(raw, allowed_ids=_ALLOWED)) == 1

    def test_rewrite_without_description_is_a_keep(self) -> None:
        """An empty description would blank the day's prose — the
        contract says empty means "leave alone", so such an entry is
        simply dropped."""
        raw = (
            '[{"activity_id": "act-walk", "action": "rewrite",'
            ' "description": "   "},'
            ' {"activity_id": "act-film", "action": "rewrite",'
            ' "location": "另一家戲院"}]'
        )
        assert _parse(raw, allowed_ids=_ALLOWED) == ()

    def test_clamps_busy_score(self) -> None:
        raw = (
            '[{"activity_id": "act-walk", "action": "rewrite",'
            ' "description": "散步", "busy_score": 7.5},'
            ' {"activity_id": "act-film", "action": "rewrite",'
            ' "description": "看片", "busy_score": -3}]'
        )
        walk, film = _parse(raw, allowed_ids=_ALLOWED)
        assert walk.busy_score == pytest.approx(1.0)
        assert film.busy_score == pytest.approx(0.0)

    def test_busy_score_garbage_falls_back_to_none(self) -> None:
        raw = (
            '[{"activity_id": "act-walk", "action": "rewrite",'
            ' "description": "散步", "busy_score": "很忙"}]'
        )
        (rewrite,) = _parse(raw, allowed_ids=_ALLOWED)
        assert rewrite.busy_score is None

    def test_numeric_string_busy_score_is_accepted(self) -> None:
        raw = (
            '[{"activity_id": "act-walk", "action": "rewrite",'
            ' "description": "散步", "busy_score": "0.8"}]'
        )
        (rewrite,) = _parse(raw, allowed_ids=_ALLOWED)
        assert rewrite.busy_score == pytest.approx(0.8)

    def test_duplicate_ids_keep_the_first_verdict(self) -> None:
        raw = (
            '[{"activity_id": "act-walk", "action": "rewrite",'
            ' "description": "第一版"},'
            ' {"activity_id": "act-walk", "action": "rewrite",'
            ' "description": "第二版"}]'
        )
        (rewrite,) = _parse(raw, allowed_ids=_ALLOWED)
        assert rewrite.description == "第一版"

    def test_truncates_overlong_description(self) -> None:
        long_text = "走" * 400
        raw = (
            '[{"activity_id": "act-walk", "action": "rewrite",'
            f' "description": "{long_text}"}}]'
        )
        (rewrite,) = _parse(raw, allowed_ids=_ALLOWED)
        assert 0 < len(rewrite.description) <= 120


class TestBuildPrompt:
    def _rendered(self, language: str = "zh-TW") -> str:
        return _build_prompt(
            character=_character(),
            activities=(_walk(), _film()),
            weather_context=_WEATHER_BLOCK,
            now=_now(),
            local_tz=TAIPEI,
            operator_primary_language=language,
        )

    def test_contains_persona_and_weather_facts(self) -> None:
        rendered = self._rendered()
        assert "Airi" in rendered
        assert "怕生" in rendered
        assert "現在：晴朗" in rendered

    def test_lists_every_activity_with_local_civil_time(self) -> None:
        rendered = self._rendered()
        assert "act-walk" in rendered
        assert "act-film" in rendered
        # 06:00Z / 07:30Z rendered in Asia/Taipei, not UTC.
        assert "14:00" in rendered
        assert "15:00" in rendered
        assert "15:30" in rendered
        assert "17:30" in rendered
        assert "撐著傘在河堤邊躲雨慢走" in rendered
        assert "河堤公園" in rendered
        assert "0.60" in rendered

    def test_states_the_current_local_time(self) -> None:
        assert "14:30" in self._rendered()

    def test_asks_for_a_json_array_verdict_per_activity(self) -> None:
        rendered = self._rendered()
        assert "activity_id" in rendered
        assert '"keep"' in rendered
        assert '"rewrite"' in rendered

    def test_prepends_operator_language_hint(self) -> None:
        rendered = self._rendered("en-US")
        assert rendered.startswith("玩家可見自然語言輸出語言")
        assert "en-US" in rendered

    def test_omits_language_hint_when_tag_is_blank(self) -> None:
        rendered = self._rendered("")
        assert "玩家可見自然語言輸出語言" not in rendered


class TestLLMScheduleWeatherDriftJudge:
    @pytest.mark.asyncio
    async def test_happy_path_returns_rewrites(self) -> None:
        model = _StubModel(
            '[{"activity_id": "act-walk", "action": "rewrite",'
            ' "description": "在河堤邊散步曬太陽"},'
            ' {"activity_id": "act-film", "action": "keep"}]',
        )
        judge = LLMScheduleWeatherDriftJudge(model)
        result = await judge.judge(
            character=_character(),
            activities=(_walk(), _film()),
            weather_context=_WEATHER_BLOCK,
            now=_now(),
            local_tz=TAIPEI,
        )
        assert len(result) == 1
        assert result[0].activity_id == "act-walk"
        assert len(model.prompts) == 1
        assert "現在：晴朗" in model.prompts[0]

    @pytest.mark.asyncio
    async def test_llm_error_is_fail_soft(self) -> None:
        """A flaky backend must never break the tick — the day simply
        stays as planned."""
        judge = LLMScheduleWeatherDriftJudge(_RaisingModel())
        result = await judge.judge(
            character=_character(),
            activities=(_walk(),),
            weather_context=_WEATHER_BLOCK,
            now=_now(),
            local_tz=TAIPEI,
        )
        assert result == ()

    @pytest.mark.asyncio
    async def test_blank_response_returns_empty(self) -> None:
        judge = LLMScheduleWeatherDriftJudge(_StubModel(""))
        result = await judge.judge(
            character=_character(),
            activities=(_walk(),),
            weather_context=_WEATHER_BLOCK,
            now=_now(),
            local_tz=TAIPEI,
        )
        assert result == ()

    @pytest.mark.asyncio
    async def test_fake_provider_short_circuits_without_calling_model(
        self,
    ) -> None:
        model = _StubModel("[]")
        judge = LLMScheduleWeatherDriftJudge(
            provider=_ActiveProvider(model, fake=True),
            feature_key="schedule_weather_drift",
        )
        result = await judge.judge(
            character=_character(),
            activities=(_walk(),),
            weather_context=_WEATHER_BLOCK,
            now=_now(),
            local_tz=TAIPEI,
        )
        assert result == ()
        assert model.prompts == []

    @pytest.mark.asyncio
    async def test_no_activities_skips_the_call(self) -> None:
        model = _StubModel("[]")
        judge = LLMScheduleWeatherDriftJudge(model)
        result = await judge.judge(
            character=_character(),
            activities=(),
            weather_context=_WEATHER_BLOCK,
            now=_now(),
            local_tz=TAIPEI,
        )
        assert result == ()
        assert model.prompts == []

    @pytest.mark.asyncio
    async def test_missing_weather_facts_skip_the_call(self) -> None:
        """Without a weather block there is nothing to contradict —
        paying for a judgement would be pure waste."""
        model = _StubModel("[]")
        judge = LLMScheduleWeatherDriftJudge(model)
        result = await judge.judge(
            character=_character(),
            activities=(_walk(),),
            weather_context="   ",
            now=_now(),
            local_tz=TAIPEI,
        )
        assert result == ()
        assert model.prompts == []


class TestNullScheduleWeatherDriftJudge:
    @pytest.mark.asyncio
    async def test_always_returns_empty(self) -> None:
        judge = NullScheduleWeatherDriftJudge()
        result = await judge.judge(
            character=_character(),
            activities=(_walk(), _film()),
            weather_context=_WEATHER_BLOCK,
            now=_now(),
            local_tz=TAIPEI,
        )
        assert result == ()
