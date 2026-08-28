from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace

import pytest

from kokoro_link.contracts.novelty_gate import NoveltyGateContext, NoveltyVerdict
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


def test_diversity_evidence_block_renders_the_script_mix_lines() -> None:
    """FC3 — the diversity channel must not silently drop the mix lines.

    ``script_mix_lines`` is the deterministic evidence the
    ``language_mismatch`` axis is meant to weigh, and
    :class:`ReplyDiversityEvidence` has carried it since QG5. Chat happens
    to also pass it through ``mechanical_evidence_lines``, which is why the
    omission here was invisible: a surface that fills only the diversity
    field (every non-chat adopter) had its evidence thrown away between the
    context and the prompt.
    """
    mix = (
        "近 3 則輸出共 120 個文字字元，組成：中日韓 55%、拉丁字母 45%、其他 0%。",
        "其中 2 則的拉丁字母占比超過 40%：第 2 則 61%、第 3 則 48%。",
    )
    context = replace(
        _context(),
        diversity_evidence=replace(
            _context().diversity_evidence, language_mix_lines=mix,
        ),
        # Deliberately empty: the parallel channel chat uses must not be
        # what makes this pass.
        mechanical_evidence_lines=(),
    )

    prompt = _build_prompt(context)

    for line in mix:
        assert line in prompt
    block = prompt.split("統計多樣性證據：", 1)[1]
    assert "language_mix:" in block


def test_diversity_evidence_block_caps_the_script_mix_lines_like_frequency() -> None:
    """Same bound and same shape as the frequency lines beside them —
    evidence competes with the material the reply is actually about."""
    context = replace(
        _context(),
        diversity_evidence=replace(
            _context().diversity_evidence,
            language_mix_lines=tuple(f"混用第 {i} 則" for i in range(1, 10)),
        ),
    )

    prompt = _build_prompt(context)

    assert prompt.count("- language_mix: ") == 6
    assert "混用第 7 則" not in prompt


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
    assert verdict.hard_fail is False


class _FakeRoutedProvider:
    """A live provider whose per-call resolution lands on the fake backend."""

    async def is_fake(
        self,
        feature_key=None,  # noqa: ANN001
        *,
        character=None,  # noqa: ANN001
        operator_id=None,  # noqa: ANN001
        content_tolerance=None,  # noqa: ANN001
    ) -> bool:
        return True


@pytest.mark.asyncio
async def test_llm_novelty_gate_reports_unrouted_when_no_real_judge_routes() -> None:
    """Not ``pass_open``: an unrouted call is not a broken judge, and the
    orchestrator must keep it off the scrape instead of counting a
    fail-open. Routing is DB-backed and per-call, so this is the gate's own
    answer — bootstrap cannot know it."""
    gate = LLMNoveltyGate(
        provider=_FakeRoutedProvider(),
        feature_key="novelty_gate",
    )

    verdict = await gate.evaluate(_context())

    assert verdict.passes is True
    assert verdict.unrouted is True
    assert not verdict.gate_metadata.get("error")


# --------------------------------------------------------------------- #
# QG1 — hard failure axes. The 2026-08-26 feed incident (image-prompt tag
# soup inside the post body, hard-cut at 280 chars, no image) walked past
# all five soft axes because none of them look at structure, language,
# truncation or the accompanying tool prompt.
# --------------------------------------------------------------------- #


_HARD_AXES = (
    "structural_leak",
    "language_mismatch",
    "visible_truncation",
    "tool_prompt_defect",
)


def _hard_fail_context() -> NoveltyGateContext:
    return NoveltyGateContext(
        character_id="c1",
        operator_id="u1",
        response_text="今天的光很好，1girl, solo, cafe, rain, masterpiece, best qu",
        operator_primary_language="繁體中文",
        tool_prompt_lines=("image_prompt: （空）",),
        mechanical_evidence_lines=("正文長度 612 超過上限 280，疑似混入非正文內容。",),
    )


def test_novelty_gate_prompt_renders_hard_fail_blocks() -> None:
    prompt = _build_prompt(_hard_fail_context())

    assert "玩家主要語言" in prompt
    assert "繁體中文" in prompt
    assert "隨附工具 prompt" in prompt
    assert "image_prompt: （空）" in prompt
    assert "機械層證據" in prompt
    assert "正文長度 612 超過上限 280" in prompt


def test_novelty_gate_prompt_renders_empty_hard_fail_blocks_as_none() -> None:
    """Every existing call site still constructs the context without the
    new fields; those must render the same 「（無）」 placeholder the other
    optional blocks use rather than raising or emitting a blank block."""
    prompt = _build_prompt(_context())

    section = prompt.split("玩家主要語言：", 1)[1]
    assert section.lstrip().startswith("（無）")
    assert "隨附工具 prompt：\n- （無）" in prompt
    assert "機械層證據：\n- （無）" in prompt


def test_novelty_gate_prompt_keeps_full_tool_prompt_for_leak_comparison() -> None:
    """A tag string clipped at the soft-axis 260-char budget would hide the
    tail that leaked into the body — the exact comparison the judge needs."""
    tag_soup = ", ".join(f"tag_{index:03d}" for index in range(40))
    context = NoveltyGateContext(
        character_id="c1",
        operator_id="u1",
        response_text="今天的光很好。",
        tool_prompt_lines=(f"image_prompt: {tag_soup}",),
    )

    prompt = _build_prompt(context)

    assert "tag_039" in prompt


def test_novelty_gate_rubric_teaches_the_four_hard_axes() -> None:
    prompt = _build_prompt(_hard_fail_context())

    for axis in _HARD_AXES:
        assert f"{axis}=true" in prompt
    # The tool prompts are legitimately English; conflating them with the
    # body is how language_mismatch would fire on every illustrated post.
    assert "絕不能因此判 language_mismatch" in prompt
    # Hard failures silence a whole background post, so the rubric is
    # deliberately narrow.
    assert "一眼可指認的破口才判 true" in prompt
    for axis in _HARD_AXES:
        assert axis in prompt.split("JSON 形狀固定為", 1)[1]


def test_verdict_hard_fail_is_false_when_only_soft_axes_fire() -> None:
    verdict = NoveltyVerdict(passes=False, over_warm=True)

    assert verdict.passes is False
    assert verdict.hard_fail is False


@pytest.mark.parametrize("axis", _HARD_AXES)
def test_verdict_hard_fail_follows_each_hard_axis(axis: str) -> None:
    verdict = NoveltyVerdict(passes=True, **{axis: True})

    assert verdict.hard_fail is True
    assert verdict.passes is False


def test_verdict_defaults_leave_every_hard_axis_false() -> None:
    verdict = NoveltyVerdict(passes=True)

    assert verdict.hard_fail is False
    assert all(getattr(verdict, axis) is False for axis in _HARD_AXES)
    assert NoveltyVerdict.pass_open("fake provider").hard_fail is False


@pytest.mark.asyncio
async def test_llm_novelty_gate_parses_hard_axes_into_verdict_and_metadata() -> None:
    gate = LLMNoveltyGate(
        model=_Model(
            '{"passes":false,"lacks_novelty":false,'
            '"imagery_relapse":false,"register_mismatch":false,'
            '"over_warm":false,"formulaic":false,'
            '"structural_leak":true,"language_mismatch":false,'
            '"visible_truncation":true,"tool_prompt_defect":true,'
            '"feedback":"正文混進了 image prompt 的 tag 串，重寫成純粹的貼文。"}',
        ),
    )

    verdict = await gate.evaluate(_hard_fail_context())

    assert verdict.passes is False
    assert verdict.hard_fail is True
    assert verdict.structural_leak is True
    assert verdict.language_mismatch is False
    assert verdict.visible_truncation is True
    assert verdict.tool_prompt_defect is True
    assert verdict.gate_metadata["hard_fail"] is True
    assert verdict.gate_metadata["structural_leak"] is True
    assert verdict.gate_metadata["language_mismatch"] is False


@pytest.mark.asyncio
async def test_llm_novelty_gate_flips_passes_when_only_a_hard_axis_fires() -> None:
    """A judge that fires a hard axis but still writes passes=true must not
    be believed — same derivation the soft axes already get."""
    gate = LLMNoveltyGate(
        model=_Model(
            '{"passes":true,"lacks_novelty":false,'
            '"imagery_relapse":false,"register_mismatch":false,'
            '"over_warm":false,"formulaic":false,'
            '"structural_leak":false,"language_mismatch":true,'
            '"visible_truncation":false,"tool_prompt_defect":false,'
            '"feedback":"整段回成英文，用玩家的繁體中文重寫。"}',
        ),
    )

    verdict = await gate.evaluate(_hard_fail_context())

    assert verdict.passes is False
    assert verdict.hard_fail is True
    assert verdict.language_mismatch is True


@pytest.mark.asyncio
async def test_llm_novelty_gate_treats_missing_hard_axes_as_not_fired() -> None:
    """A prompt pack that has not grown the hard axes yet (a stale tuned
    overlay on a hosted deployment) omits the four keys entirely. That is a
    pack lag, not a defect — it must not block the surface."""
    gate = LLMNoveltyGate(
        model=_Model(
            '{"passes":true,"lacks_novelty":false,'
            '"imagery_relapse":false,"register_mismatch":false,'
            '"over_warm":false,"formulaic":false,"feedback":""}',
        ),
    )

    verdict = await gate.evaluate(_hard_fail_context())

    assert verdict.passes is True
    assert verdict.hard_fail is False
    assert verdict.gate_metadata["hard_fail"] is False


# --------------------------------------------------------------------- #
# TC — temporal_inconsistency, the tenth axis.
#
# 2026-08-27 incident: the player said 「要回家了」 yesterday afternoon and
# the character asked 「回家了嗎？」 the next morning. Every layer held the
# timestamps; none of them reached the judge, so no axis could see it.
# --------------------------------------------------------------------- #


def _stale_followup_context() -> NoveltyGateContext:
    return NoveltyGateContext(
        character_id="c1",
        operator_id="u1",
        response_text="回家了嗎？路上小心喔",
        latest_user_message="我要回家了",
        operator_primary_language="繁體中文",
        temporal_context_lines=(
            "現在：2026-08-27 09:12 CST（上午）",
            "玩家最後一次說話「我要回家了」：2026-08-26 17:30（下午）｜約 16 小時前（1 天前）",
        ),
    )


def test_novelty_gate_prompt_renders_the_temporal_block() -> None:
    prompt = _build_prompt(_stale_followup_context())

    assert "時間座標" in prompt
    assert "約 16 小時前（1 天前）" in prompt


def test_novelty_gate_prompt_renders_absent_temporal_block_as_none() -> None:
    """Every pre-existing call site builds a context without the field.

    The rubric pins the axis false on 「（無）」, so this placeholder is the
    fail-safe that keeps un-wired surfaces provably unaffected."""
    prompt = _build_prompt(_context())

    block = prompt.split("時間座標：", 1)[1].strip()
    assert block.startswith("- （無）")


def test_novelty_gate_rubric_teaches_the_temporal_axis_and_its_fail_safe() -> None:
    prompt = _build_prompt(_stale_followup_context())

    assert "temporal_inconsistency=true" in prompt
    assert "temporal_inconsistency" in prompt.split("JSON 形狀固定為", 1)[1]
    # The fail-safe: no anchors, no verdict.
    assert "「時間座標」欄是（無）時，temporal_inconsistency 一律 false" in prompt
    # LLM-first: the rubric must not hand the judge an hour threshold.
    assert "不是某個固定時數" in prompt or "不要心裡定一個小時數當紅線" in prompt


@pytest.mark.asyncio
async def test_llm_novelty_gate_parses_the_temporal_axis_as_a_hard_failure() -> None:
    gate = LLMNoveltyGate(
        model=_Model(
            '{"passes":false,"lacks_novelty":false,'
            '"imagery_relapse":false,"register_mismatch":false,'
            '"over_warm":false,"formulaic":false,'
            '"structural_leak":false,"language_mismatch":false,'
            '"visible_truncation":false,"tool_prompt_defect":false,'
            '"temporal_inconsistency":true,'
            '"feedback":"玩家是 16 小時前說要回家的，改問「昨天有順利到家嗎」。"}',
        ),
    )

    verdict = await gate.evaluate(_stale_followup_context())

    assert verdict.temporal_inconsistency is True
    # Hard, so a background surface withholds rather than sends.
    assert verdict.hard_fail is True
    assert verdict.passes is False
    assert verdict.gate_metadata["temporal_inconsistency"] is True
    assert verdict.fired_axes == ("temporal_inconsistency",)


@pytest.mark.asyncio
async def test_llm_novelty_gate_treats_a_missing_temporal_axis_as_not_fired() -> None:
    """A tuned overlay that has not grown the axis yet omits the key; that
    is a pack lag, not a defect, and must not block the surface."""
    gate = LLMNoveltyGate(
        model=_Model('{"passes":true,"lacks_novelty":false,"feedback":""}'),
    )

    verdict = await gate.evaluate(_stale_followup_context())

    assert verdict.passes is True
    assert verdict.temporal_inconsistency is False
    assert verdict.hard_fail is False
