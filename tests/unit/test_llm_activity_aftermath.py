"""Parser-level tests for the LLM-backed activity-aftermath judge.

Exercises the response extraction and sanitisation logic in
:mod:`kokoro_link.infrastructure.schedule.llm_aftermath` without hitting
any actual LLM — a stub chat model returns canned responses.

We deliberately don't assert on the *content* of the LLM's judgement
(per the project's top directive: no keyword enumeration). Instead we
verify the *contract* the adapter promises to the rest of the system:

- A well-formed two-line response yields both fields.
- Blank / fenced / partially-labelled output degrades gracefully to an
  empty residue rather than crashing the memorialiser.
- An overshot emotion tag (model wrote a sentence) is dropped so it
  can't poison memory tags downstream.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone

import pytest

from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.schedule.llm_aftermath import (
    LLMActivityAftermathJudge,
    NullActivityAftermathJudge,
    _parse,
)


UTC = timezone.utc


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
        summary="高中生",
        personality=["怕生"],
        interests=["甜點"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=20, trust=50, energy=80,
        ),
    )


def _activity() -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(2026, 5, 15, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 5, 15, 10, 0, tzinfo=UTC),
        description="和大媽聊天",
        category="社交",
        busy_score=0.6,
        location="家門口",
        companion_names=("鄰居大媽",),
    )


class TestParse:
    def test_extracts_both_labels(self) -> None:
        raw = "情緒尾韻：被一直追問感情狀況，很煩\n情緒標籤：煩躁"
        result = _parse(raw)
        assert result.residue_summary == "被一直追問感情狀況，很煩"
        assert result.emotion_tag == "煩躁"
        assert result.is_empty is False

    def test_accepts_halfwidth_colon(self) -> None:
        raw = "情緒尾韻: 心情很好\n情緒標籤: 雀躍"
        result = _parse(raw)
        assert result.residue_summary == "心情很好"
        assert result.emotion_tag == "雀躍"

    def test_blank_input_is_empty(self) -> None:
        assert _parse("").is_empty
        assert _parse("   \n  \n").is_empty

    def test_strips_code_fences(self) -> None:
        raw = "```\n情緒尾韻：聊到甜點心情很好\n情緒標籤：雀躍\n```"
        result = _parse(raw)
        assert result.residue_summary == "聊到甜點心情很好"
        assert result.emotion_tag == "雀躍"

    def test_strips_surrounding_quotes(self) -> None:
        raw = "情緒尾韻：「被追問很煩」\n情緒標籤：「煩躁」"
        result = _parse(raw)
        assert result.residue_summary == "被追問很煩"
        assert result.emotion_tag == "煩躁"

    def test_only_residue_label_present(self) -> None:
        raw = "情緒尾韻：被追問很煩"
        result = _parse(raw)
        assert result.residue_summary == "被追問很煩"
        assert result.emotion_tag == ""

    def test_only_emotion_label_present(self) -> None:
        raw = "情緒標籤：煩躁"
        result = _parse(raw)
        assert result.residue_summary == ""
        assert result.emotion_tag == "煩躁"

    def test_truncates_overlong_residue(self) -> None:
        long_text = "煩" * 200
        raw = f"情緒尾韻：{long_text}\n情緒標籤：煩躁"
        result = _parse(raw)
        # _MAX_RESIDUE_CHARS = 120 (widened for non-CJK) → truncated + ellipsis
        assert len(result.residue_summary) <= 121
        assert result.residue_summary.endswith("…")
        assert result.emotion_tag == "煩躁"

    def test_drops_overshot_emotion_tag(self) -> None:
        """A whole sentence as tag means the model misread the schema —
        keeping it would poison memory tags downstream, so the adapter
        drops it rather than truncates. The cap is widened for non-CJK
        labels, so the overshoot sentence here is comfortably past it."""
        long_tag = "被同事一直追問感情狀況追問到我整個人都煩躁到不行的那種疲憊感受"
        raw = f"情緒尾韻：被同事煩到頭痛\n情緒標籤：{long_tag}"
        result = _parse(raw)
        assert result.residue_summary == "被同事煩到頭痛"
        assert result.emotion_tag == ""

    def test_handles_blank_label_values(self) -> None:
        """Model returns the schema with empty fields → empty result."""
        raw = "情緒尾韻：\n情緒標籤："
        result = _parse(raw)
        assert result.is_empty


class TestLLMActivityAftermathJudge:
    @pytest.mark.asyncio
    async def test_happy_path_returns_parsed_aftermath(self) -> None:
        model = _StubModel(
            "情緒尾韻：被大媽追問感情很煩\n情緒標籤：煩躁",
        )
        judge = LLMActivityAftermathJudge(model)
        result = await judge.judge(
            character=_character(), activity=_activity(),
        )
        assert result.residue_summary == "被大媽追問感情很煩"
        assert result.emotion_tag == "煩躁"
        # Prompt must include persona axes so the LLM judges per-persona
        # (anti-enumeration). We don't pin exact wording, just verify the
        # persona block was injected at all.
        assert len(model.prompts) == 1
        rendered = model.prompts[0]
        assert "怕生" in rendered  # personality axis
        assert "甜點" in rendered  # interests axis
        assert "和大媽聊天" in rendered  # activity description

    @pytest.mark.asyncio
    async def test_llm_error_is_fail_soft(self) -> None:
        """A flaky backend must never block schedule history — the
        memorialiser falls back to the bare-activity memory path."""
        judge = LLMActivityAftermathJudge(_RaisingModel())
        result = await judge.judge(
            character=_character(), activity=_activity(),
        )
        assert result.is_empty

    @pytest.mark.asyncio
    async def test_blank_response_returns_empty(self) -> None:
        judge = LLMActivityAftermathJudge(_StubModel(""))
        result = await judge.judge(
            character=_character(), activity=_activity(),
        )
        assert result.is_empty


class TestNullActivityAftermathJudge:
    @pytest.mark.asyncio
    async def test_always_returns_empty(self) -> None:
        judge = NullActivityAftermathJudge()
        result = await judge.judge(
            character=_character(), activity=_activity(),
        )
        assert result.is_empty
