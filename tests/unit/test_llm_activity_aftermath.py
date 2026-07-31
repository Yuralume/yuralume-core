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
from kokoro_link.domain.entities.schedule import (
    OPERATOR_CONFIRMED_SHARED_ROLE,
    OPERATOR_INVITE_PENDING_ROLE,
    ScheduleActivity,
)
from kokoro_link.domain.services.shared_activity_evidence import (
    OperatorPresenceEvidence,
    SharedActivityEvidence,
    SharedClaimPolicy,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.schedule.llm_aftermath import (
    LLMActivityAftermathJudge,
    NullActivityAftermathJudge,
    _build_prompt,
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


def _evidence(
    policy: SharedClaimPolicy,
    *,
    role: str = OPERATOR_INVITE_PENDING_ROLE,
    presence: OperatorPresenceEvidence = OperatorPresenceEvidence.BEFORE_ACTIVITY,
) -> SharedActivityEvidence:
    return SharedActivityEvidence(
        policy=policy,
        operator_role=role,
        operator_display_name="木木",
        presence=presence,
    )


class TestSharedActivityBlock:
    """CF4 — the shared-completion discipline is carried by the prompt.

    We assert the *rule and the facts are present*, never that a
    particular sentence comes back: the wording of the memory is the
    model's judgement, which is exactly why no keyword rewriting happens
    on our side.
    """

    def test_ordinary_activity_keeps_the_two_line_contract(self) -> None:
        prompt = _build_prompt(character=_character(), activity=_activity())

        assert "實際情形" not in prompt
        assert "使用者稱呼" not in prompt

    def test_solo_only_forbids_a_shared_completion_claim(self) -> None:
        prompt = _build_prompt(
            character=_character(),
            activity=_activity(),
            evidence=_evidence(SharedClaimPolicy.SOLO_ONLY),
        )

        assert "木木" in prompt                       # who
        assert "從頭到尾沒有答應" in prompt            # commitment state fact
        assert "活動進行期間完全沒有訊息" in prompt     # presence fact
        assert "絕對禁止" in prompt                    # the rule
        assert "實際情形" in prompt                    # the extra output line
        # The description is a plan, not a record — the model is told so
        # explicitly, because the plan text is what used to leak through.
        assert "計畫" in prompt

    def test_unverified_asks_for_neutral_wording_in_both_directions(self) -> None:
        prompt = _build_prompt(
            character=_character(),
            activity=_activity(),
            evidence=_evidence(
                SharedClaimPolicy.UNVERIFIED,
                role=OPERATOR_CONFIRMED_SHARED_ROLE,
                presence=OperatorPresenceEvidence.ONLY_AFTER,
            ),
        )

        assert "使用者當初確實答應過要一起" in prompt
        assert "無從得知" in prompt
        assert "放了鴿子" in prompt   # must not assert the negative either
        assert "實際情形" in prompt

    def test_verified_allows_the_shared_framing(self) -> None:
        prompt = _build_prompt(
            character=_character(),
            activity=_activity(),
            evidence=_evidence(
                SharedClaimPolicy.VERIFIED,
                role=OPERATOR_CONFIRMED_SHARED_ROLE,
                presence=OperatorPresenceEvidence.DURING_ACTIVITY,
            ),
        )

        assert "確實有互動紀錄" in prompt
        assert "可以寫成兩人一起經歷的敘述" in prompt

    def test_unknown_history_is_stated_as_ignorance_not_absence(self) -> None:
        prompt = _build_prompt(
            character=_character(),
            activity=_activity(),
            evidence=_evidence(
                SharedClaimPolicy.UNVERIFIED,
                role=OPERATOR_CONFIRMED_SHARED_ROLE,
                presence=OperatorPresenceEvidence.UNKNOWN,
            ),
        )

        assert "查不到" in prompt
        assert "不是「他沒來」" in prompt

    @pytest.mark.asyncio
    async def test_judge_threads_evidence_into_the_prompt(self) -> None:
        model = _StubModel("實際情形：本來想約他，最後自己去了")
        judge = LLMActivityAftermathJudge(model)

        result = await judge.judge(
            character=_character(),
            activity=_activity(),
            evidence=_evidence(SharedClaimPolicy.SOLO_ONLY),
        )

        assert result.factual_summary == "本來想約他，最後自己去了"
        assert result.is_empty is False
        assert "絕對禁止" in model.prompts[0]


class TestParseFactualLine:
    def test_extracts_the_factual_line(self) -> None:
        raw = (
            "情緒尾韻：有點悶\n"
            "情緒標籤：失落\n"
            "實際情形：本來想約木木一起去刨冰店，等不到他，最後自己去了"
        )
        result = _parse(raw)

        assert result.factual_summary == "本來想約木木一起去刨冰店，等不到他，最後自己去了"
        assert result.residue_summary == "有點悶"

    def test_factual_line_alone_is_not_empty(self) -> None:
        """No emotional residue but a factual account is still a usable
        answer — it is the only thing that lets the memory be written."""
        result = _parse("實際情形：自己去了")

        assert result.is_empty is False
        assert result.factual_summary == "自己去了"

    def test_truncates_rather_than_drops_an_overlong_account(self) -> None:
        result = _parse("實際情形：" + "去" * 400)

        assert len(result.factual_summary) <= 201
        assert result.factual_summary.endswith("…")
