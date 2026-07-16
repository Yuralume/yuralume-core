from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from kokoro_link.contracts.novelty_gate import NoveltyGateContext
from kokoro_link.contracts.register_profile import RegisterProfile
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.infrastructure.prompt.llm_novelty_gate import (
    LLMNoveltyGate,
    _build_prompt,
)
from kokoro_link.infrastructure.prompt.null_novelty_gate import NullNoveltyGate


class _Model:
    provider_id = "unit-provider"
    supports_vision = False

    def __init__(self, response: str | Exception) -> None:
        self.response = response
        self.prompts: list[str] = []

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        del image_urls, model
        self.prompts.append(prompt)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        del prompt, image_urls, model
        if False:
            yield ""

    async def list_models(self) -> list[str]:
        return []


def _context() -> NoveltyGateContext:
    return NoveltyGateContext(
        character_id="c1",
        operator_id="u1",
        latest_user_message="跟我說說妳今天發生的事情吧",
        response_text="今天咖啡很香，水光很安靜。",
        known_material=("15 分鐘前動態牆：今天咖啡很香。",),
        recent_self_lines=("我剛剛也說水光很安靜。",),
        self_repetition_hint="最近常重複安靜、水光、像霧一樣的意象。",
        content_tolerance="frontier",
        register_profile=RegisterProfile(
            axes={
                "emotional_intensity": 0.1,
                "seriousness": 0.2,
                "intimacy": 0.2,
                "humor_latitude": 0.5,
                "help_seeking": 0.0,
            },
            confidence=0.8,
            note="日常閒聊",
        ),
        diversity_evidence=ReplyDiversityEvidence(
            assistant_line_count=4,
            max_self_similarity=0.91,
            mean_self_similarity=0.74,
            self_repetition_hint="最近常重複安靜、水光、像霧一樣的意象。",
            phrase_frequency_lines=("同一模式近 8 輪出現 3 次。",),
        ),
        persona_context=("性格：嘴硬但關心人。",),
    )


def test_novelty_gate_prompt_contains_candidate_and_material() -> None:
    prompt = _build_prompt(_context())

    assert "候選回覆" in prompt
    assert "已知素材" in prompt
    assert "最近已說過" in prompt
    assert "被點名的重複傾向" in prompt
    assert "本輪語域剖面" in prompt
    assert "統計多樣性證據" in prompt
    assert "角色語氣基準" in prompt
    assert "max_self_similarity=0.910" in prompt
    assert "今天咖啡很香" in prompt
    assert "frontier" in prompt


@pytest.mark.asyncio
async def test_llm_novelty_gate_parses_failing_verdict_with_metadata() -> None:
    gate = LLMNoveltyGate(
        model=_Model(
            '{"passes":false,"lacks_novelty":true,'
            '"imagery_relapse":true,"register_mismatch":true,'
            '"over_warm":true,"formulaic":true,'
            '"feedback":"不要重講咖啡，補一件此刻的小事。"}',
        ),
    )

    verdict = await gate.evaluate(_context())

    assert verdict.passes is False
    assert verdict.lacks_novelty is True
    assert verdict.imagery_relapse is True
    assert verdict.register_mismatch is True
    assert verdict.over_warm is True
    assert verdict.formulaic is True
    assert verdict.feedback == "不要重講咖啡，補一件此刻的小事。"
    assert verdict.gate_metadata["provider_id"] == "unit-provider"
    assert verdict.gate_metadata["model_id"] == "unit-provider"


@pytest.mark.asyncio
async def test_llm_novelty_gate_parses_passing_verdict() -> None:
    gate = LLMNoveltyGate(
        model=_Model(
            '{"passes":true,"lacks_novelty":false,'
            '"imagery_relapse":false,"register_mismatch":false,'
            '"over_warm":false,"formulaic":false,"feedback":""}',
        ),
    )

    verdict = await gate.evaluate(_context())

    assert verdict.passes is True
    assert verdict.lacks_novelty is False
    assert verdict.imagery_relapse is False
    assert verdict.register_mismatch is False
    assert verdict.over_warm is False
    assert verdict.formulaic is False


@pytest.mark.asyncio
async def test_llm_novelty_gate_derives_passes_from_axes_when_model_disagrees() -> None:
    gate = LLMNoveltyGate(
        model=_Model(
            '{"passes":true,"lacks_novelty":false,'
            '"imagery_relapse":false,"register_mismatch":false,'
            '"over_warm":true,"formulaic":false,"feedback":"收掉過度安撫。"}',
        ),
    )

    verdict = await gate.evaluate(_context())

    assert verdict.passes is False
    assert verdict.over_warm is True


@pytest.mark.asyncio
@pytest.mark.parametrize("response", ["not json", '{"passes":"false"}'])
async def test_llm_novelty_gate_fails_open_for_bad_output(response: str) -> None:
    verdict = await LLMNoveltyGate(model=_Model(response)).evaluate(_context())

    assert verdict.passes is True
    assert verdict.gate_metadata["error"]


@pytest.mark.asyncio
async def test_llm_novelty_gate_fails_open_on_provider_error() -> None:
    verdict = await LLMNoveltyGate(model=_Model(RuntimeError("boom"))).evaluate(
        _context(),
    )

    assert verdict.passes is True
    assert "boom" in verdict.gate_metadata["error"]


@pytest.mark.asyncio
async def test_null_novelty_gate_passes() -> None:
    verdict = await NullNoveltyGate().evaluate(_context())

    assert verdict.passes is True
