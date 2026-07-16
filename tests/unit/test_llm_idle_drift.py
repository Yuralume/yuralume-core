"""Parser-level tests for the LLM-backed idle-drift judge.

Same shape as ``test_llm_activity_aftermath`` — exercises the response
extraction and sanitisation logic without hitting a real LLM. A stub
chat model returns canned responses.

We test the *contract* the adapter promises, never the LLM's content
choices (per project's top directive — no keyword enumeration):

- A well-formed multi-line response yields every parsed field.
- Numeric deltas are clamped to safe ranges so a runaway model can't
  swing affection by ±50.
- Overshot emotion tag (sentence instead of label) is dropped so it
  can't poison ``CharacterState.emotion`` downstream.
- Blank / partial / fenced output degrades to empty drift cleanly.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.state.llm_idle_drift import (
    LLMIdleDriftJudge,
    NullIdleDriftJudge,
    _parse,
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


def _character() -> Character:
    return Character.create(
        name="Airi",
        summary="高中生，黏人但嘴硬",
        personality=["傲嬌"],
        interests=["甜點"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=60, fatigue=20, trust=50, energy=80,
        ),
    )


class TestParse:
    def test_extracts_all_fields(self) -> None:
        raw = (
            "情緒：鬧彆扭\n"
            "好感變化：-3\n"
            "精力變化：0\n"
            "疲勞變化：1\n"
            "內心：三天都沒理我\n"
            "短期意圖：等對方先低頭"
        )
        result = _parse(raw)
        assert result.emotion == "鬧彆扭"
        assert result.affection_delta == -3
        assert result.energy_delta == 0
        assert result.fatigue_delta == 1
        assert result.note == "三天都沒理我"
        assert result.current_intent == "等對方先低頭"
        assert result.is_empty is False

    def test_blank_input_is_empty(self) -> None:
        assert _parse("").is_empty
        assert _parse("   \n  \n").is_empty

    def test_strips_code_fences(self) -> None:
        raw = "```\n情緒：失落\n好感變化：-2\n```"
        result = _parse(raw)
        assert result.emotion == "失落"
        assert result.affection_delta == -2

    def test_clamps_excessive_affection(self) -> None:
        """A runaway model writing -50 must not swing affection by 50."""
        raw = "情緒：暴怒\n好感變化：-50"
        result = _parse(raw)
        assert result.affection_delta == -8  # _MAX_AFFECTION_DELTA

    def test_clamps_excessive_positive(self) -> None:
        raw = "情緒：雀躍\n好感變化：+999"
        result = _parse(raw)
        assert result.affection_delta == 8

    def test_drops_overshot_emotion(self) -> None:
        """Model wrote a whole sentence as the emotion tag → drop so we
        don't store a sentence as a character emotion. The cap is now
        sized for the widest shipped language (not CJK), so the sentence
        must be long enough to exceed _MAX_EMOTION_CHARS (24)."""
        raw = (
            "情緒：被冷落了整整三天之後那種既生氣又有點想念卻死不承認的複雜情緒\n"
            "好感變化：-3"
        )
        result = _parse(raw)
        assert result.emotion is None
        assert result.affection_delta == -3  # other fields still parse

    def test_truncates_overlong_note(self) -> None:
        long_note = "煩" * 200
        raw = f"情緒：煩躁\n內心：{long_note}"
        result = _parse(raw)
        assert result.note is not None
        # _MAX_NOTE_CHARS = 120 → truncated + ellipsis
        assert len(result.note) <= 121
        assert result.note.endswith("…")

    def test_handles_quoted_values(self) -> None:
        raw = '情緒：「失落」\n好感變化：「-2」\n內心：「想念」'
        result = _parse(raw)
        assert result.emotion == "失落"
        assert result.affection_delta == -2
        assert result.note == "想念"

    def test_garbage_int_becomes_zero(self) -> None:
        raw = "情緒：平靜\n好感變化：不知道"
        result = _parse(raw)
        assert result.affection_delta == 0

    def test_all_empty_values_is_empty_drift(self) -> None:
        """Model returned the schema with every field blank → no drift."""
        raw = (
            "情緒：\n好感變化：0\n精力變化：0\n疲勞變化：0\n"
            "內心：\n短期意圖："
        )
        result = _parse(raw)
        assert result.is_empty


class TestLLMIdleDriftJudge:
    @pytest.mark.asyncio
    async def test_happy_path_returns_parsed_drift(self) -> None:
        model = _StubModel(
            "情緒：鬧彆扭\n好感變化：-3\n內心：哼，三天才聯絡我",
        )
        judge = LLMIdleDriftJudge(model)
        result = await judge.judge(character=_character(), idle_minutes=4320.0)
        assert result.emotion == "鬧彆扭"
        assert result.affection_delta == -3
        assert result.note == "哼，三天才聯絡我"
        # Verify persona axes are injected — never enumerated.
        rendered = model.prompts[0]
        assert "傲嬌" in rendered  # personality
        assert "甜點" in rendered  # interests
        # Idle duration must surface so the LLM can scale the drift.
        assert "天" in rendered or "小時" in rendered

    @pytest.mark.asyncio
    async def test_llm_error_is_fail_soft(self) -> None:
        judge = LLMIdleDriftJudge(_RaisingModel())
        result = await judge.judge(character=_character(), idle_minutes=720.0)
        assert result.is_empty

    @pytest.mark.asyncio
    async def test_blank_response_returns_empty(self) -> None:
        judge = LLMIdleDriftJudge(_StubModel(""))
        result = await judge.judge(character=_character(), idle_minutes=240.0)
        assert result.is_empty

    @pytest.mark.asyncio
    async def test_renders_minutes_for_short_idle(self) -> None:
        """Under an hour stays in minutes — readable for the LLM."""
        model = _StubModel("")
        judge = LLMIdleDriftJudge(model)
        await judge.judge(character=_character(), idle_minutes=45.0)
        assert "45" in model.prompts[0]
        assert "分鐘" in model.prompts[0]


class TestNullIdleDriftJudge:
    @pytest.mark.asyncio
    async def test_always_returns_empty(self) -> None:
        judge = NullIdleDriftJudge()
        result = await judge.judge(character=_character(), idle_minutes=99999.0)
        assert result.is_empty
