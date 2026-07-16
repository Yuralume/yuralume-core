"""FusionStoryPlanner parser + fallback tests.

Pins:
- LLM-emitted JSON parses into a 6–10-beat outline (default 8) with the
  canonical opening → rising → turn → resolution monotone act order.
- Fences / preamble are stripped (smaller models tend to wrap).
- Bad shape (too few beats / non-monotone acts / no JSON / focus ids
  outside whitelist) falls back to the synthetic outline rather than
  crashing.
"""

from __future__ import annotations

import json

import pytest

from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
)
from kokoro_link.application.services.fusion_story_planner import (
    FusionStoryPlanner,
)
from kokoro_link.domain.value_objects.fusion_outline import (
    ACT_OPENING,
    ACT_RESOLUTION,
    ACT_RISING,
    ACT_TURN,
)


class _FakeModel:
    supports_vision = False
    provider_id = "fake"

    def __init__(self, response: str) -> None:
        self._response = response
        self.last_prompt: str | None = None

    async def generate(
        self,
        prompt: str,
        *,
        image_urls=None,  # noqa: ARG002
        model=None,  # noqa: ARG002
    ) -> str:
        self.last_prompt = prompt
        return self._response

    def generate_stream(
        self, prompt: str, *, image_urls=None, model=None,  # noqa: ARG002
    ):
        async def _empty():
            if False:
                yield ""
        return _empty()

    async def list_models(self) -> list[str]:
        return []


def _briefs() -> list[CharacterBrief]:
    return [
        CharacterBrief(
            character_id="a",
            name="Alice",
            summary="A 簡介",
            text="## 角色：Alice (id=a)\n- 簡介：A",
        ),
        CharacterBrief(
            character_id="b",
            name="Bob",
            summary="B 簡介",
            text="## 角色：Bob (id=b)\n- 簡介：B",
        ),
    ]


def _good_response() -> str:
    # 8-beat shape matching the planner's _DEFAULT_BEATS distribution
    # (起 1 / 承 4 / 轉 2 / 合 1). Acts are monotone-non-decreasing.
    return json.dumps(
        {
            "title": "雷雨夜",
            "premise": "他們在書店等雷雨停。",
            "theme": "encounter",
            "beats": [
                {
                    "sequence": 0,
                    "act": ACT_OPENING,
                    "title": "雨開始下",
                    "hook": "兩人各自走進同一間書店。",
                    "dramatic_question": "他們會注意到對方嗎？",
                    "target_chars": 700,
                    "focus_character_ids": ["a", "b"],
                },
                {
                    "sequence": 1,
                    "act": ACT_RISING,
                    "title": "第一次照面",
                    "hook": "兩人在同一個書架前停下。",
                    "dramatic_question": "誰先打破沉默？",
                    "target_chars": 800,
                    "focus_character_ids": ["a", "b"],
                },
                {
                    "sequence": 2,
                    "act": ACT_RISING,
                    "title": "停電",
                    "hook": "全店瞬間陷入黑暗。",
                    "dramatic_question": "他們會靠近還是退開？",
                    "target_chars": 800,
                    "focus_character_ids": ["a", "b"],
                },
                {
                    "sequence": 3,
                    "act": ACT_RISING,
                    "title": "第三人的影子",
                    "hook": "店裡傳出第三道腳步聲。",
                    "dramatic_question": "他們要不要把對方納進來？",
                    "target_chars": 800,
                    "focus_character_ids": ["a"],
                },
                {
                    "sequence": 4,
                    "act": ACT_RISING,
                    "title": "黎明前的低語",
                    "hook": "他們在停電裡聊起各自的傷。",
                    "dramatic_question": "他們會說真話嗎？",
                    "target_chars": 900,
                    "focus_character_ids": ["a", "b"],
                },
                {
                    "sequence": 5,
                    "act": ACT_TURN,
                    "title": "舊照片",
                    "hook": "翻到一張意外的照片。",
                    "dramatic_question": "他們要承認嗎？",
                    "target_chars": 900,
                    "focus_character_ids": ["a", "b"],
                },
                {
                    "sequence": 6,
                    "act": ACT_TURN,
                    "title": "代價",
                    "hook": "其中一人下了承諾，另一人沉默。",
                    "dramatic_question": "誰要承擔這個代價？",
                    "target_chars": 800,
                    "focus_character_ids": ["a", "b"],
                },
                {
                    "sequence": 7,
                    "act": ACT_RESOLUTION,
                    "title": "雨停了",
                    "hook": "他們在門口分別。",
                    "dramatic_question": "下次會主動找彼此嗎？",
                    "target_chars": 700,
                    "focus_character_ids": ["a", "b"],
                },
            ],
        },
        ensure_ascii=False,
    )


class TestPlannerParse:
    @pytest.mark.asyncio
    async def test_parses_full_outline(self) -> None:
        model = _FakeModel(_good_response())
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(prompt="一個雷雨夜", briefs=_briefs())
        assert outline.title == "雷雨夜"
        assert len(outline.beats) == 8
        # Distribution is 起1 / 承4 / 轉2 / 合1; acts must be monotone.
        assert [b.act for b in outline.beats] == [
            ACT_OPENING,
            ACT_RISING, ACT_RISING, ACT_RISING, ACT_RISING,
            ACT_TURN, ACT_TURN,
            ACT_RESOLUTION,
        ]
        assert outline.beats[1].focus_character_ids == ("a", "b")

    @pytest.mark.asyncio
    async def test_strips_code_fence(self) -> None:
        wrapped = "```json\n" + _good_response() + "\n```"
        model = _FakeModel(wrapped)
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(prompt="p", briefs=_briefs())
        assert len(outline.beats) == 8

    @pytest.mark.asyncio
    async def test_falls_back_when_below_min_beats(self) -> None:
        # Anything under _MIN_BEATS (6) is treated as malformed and the
        # synthetic fallback kicks in. Slicing to 5 verifies that edge.
        too_few = json.loads(_good_response())
        too_few["beats"] = too_few["beats"][:5]
        model = _FakeModel(json.dumps(too_few, ensure_ascii=False))
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(prompt="p", briefs=_briefs())
        # Fallback emits the canonical 8-beat synthetic template.
        assert len(outline.beats) == 8

    @pytest.mark.asyncio
    async def test_falls_back_when_acts_not_monotone(self) -> None:
        # If the LLM scrambles the act order (e.g. rising then opening),
        # the planner must reject rather than render with broken pacing.
        data = json.loads(_good_response())
        # Swap a later turn beat back to opening to break monotonicity.
        data["beats"][5]["act"] = ACT_OPENING
        model = _FakeModel(json.dumps(data, ensure_ascii=False))
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(prompt="p", briefs=_briefs())
        # Synthetic fallback should kick in; original title gone.
        assert outline.title != "雷雨夜"
        assert len(outline.beats) == 8

    @pytest.mark.asyncio
    async def test_drops_focus_ids_outside_whitelist(self) -> None:
        data = json.loads(_good_response())
        data["beats"][0]["focus_character_ids"] = ["a", "ghost"]
        model = _FakeModel(json.dumps(data, ensure_ascii=False))
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(prompt="p", briefs=_briefs())
        assert outline.beats[0].focus_character_ids == ("a",)

    @pytest.mark.asyncio
    async def test_unparseable_falls_back_to_synthetic(self) -> None:
        model = _FakeModel("not json at all")
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(prompt="提示", briefs=_briefs())
        # Synthetic outline emits the canonical 8-beat template.
        assert len(outline.beats) == 8

    @pytest.mark.asyncio
    async def test_parses_transition_fields_when_provided(self) -> None:
        data = json.loads(_good_response())
        data["beats"][0]["entry_state"] = "週六下午，街口，多視角"
        data["beats"][0]["exit_state"] = "傍晚，書店門口，視角停在 A"
        data["beats"][0]["transition_from_previous"] = "開場"
        data["beats"][1]["transition_from_previous"] = (
            "短跳躍（兩小時後）+ 場景切換到書店"
        )
        model = _FakeModel(json.dumps(data, ensure_ascii=False))
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(prompt="p", briefs=_briefs())
        assert outline.beats[0].entry_state == "週六下午，街口，多視角"
        assert outline.beats[0].exit_state == "傍晚，書店門口，視角停在 A"
        assert outline.beats[1].transition_from_previous.startswith(
            "短跳躍",
        )

    @pytest.mark.asyncio
    async def test_missing_transition_fields_default_to_empty(self) -> None:
        # Older outlines without transition keys should still parse.
        model = _FakeModel(_good_response())
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(prompt="p", briefs=_briefs())
        for beat in outline.beats:
            assert beat.entry_state == ""
            assert beat.exit_state == ""
            assert beat.transition_from_previous == ""

    @pytest.mark.asyncio
    async def test_synthetic_fallback_carries_transition_seeds(self) -> None:
        # The fallback template now seeds transition strings so the
        # writer prompt always has something to anchor on, even when
        # the LLM call failed entirely.
        model = _FakeModel("not json at all")
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(prompt="提示", briefs=_briefs())
        # First beat's transition is the conventional "開場"; later
        # beats should each have a non-empty transition spec.
        assert outline.beats[0].transition_from_previous == "開場"
        for beat in outline.beats[1:]:
            assert beat.transition_from_previous.strip() != ""
        for beat in outline.beats:
            assert beat.entry_state.strip() != ""
            assert beat.exit_state.strip() != ""


def _outline_blob(outline) -> str:
    parts = [outline.title, outline.premise]
    for beat in outline.beats:
        parts.extend([
            beat.title, beat.hook, beat.dramatic_question,
            beat.entry_state, beat.exit_state, beat.transition_from_previous,
        ])
    return " ".join(parts)


def _has_han(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _has_kana(text: str) -> bool:
    return any("぀" <= ch <= "ヿ" for ch in text)


class TestSyntheticOutlineLocalization:
    """``_FALLBACK_BEATS`` (the eight-beat all-Chinese outline used when
    the planner LLM call fails / returns garbage) hardcoded zh-TW prose
    regardless of the operator's ``primary_language``. Mirrors the
    already-landed ``llm_arc_planner._SYNTHETIC_ARC_TEMPLATES`` pattern:
    a static, intentionally LLM-free three-language template pack
    (zh-TW / en-US / ja-JP) resolved exact-tag -> language-family ->
    zh-TW fallback."""

    @pytest.mark.asyncio
    async def test_falls_back_to_zh_tw_by_default(self) -> None:
        model = _FakeModel("not json at all")
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(prompt="提示", briefs=_briefs())
        assert _has_han(_outline_blob(outline))

    @pytest.mark.asyncio
    async def test_localizes_to_english(self) -> None:
        model = _FakeModel("not json at all")
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(
            prompt="a prompt", briefs=_briefs(),
            operator_primary_language="en-US",
        )
        blob = _outline_blob(outline)
        assert not _has_han(blob), blob
        assert len(outline.beats) == 8

    @pytest.mark.asyncio
    async def test_localizes_to_japanese(self) -> None:
        model = _FakeModel("not json at all")
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(
            prompt="a prompt", briefs=_briefs(),
            operator_primary_language="ja-JP",
        )
        blob = _outline_blob(outline)
        assert _has_kana(blob), blob
        assert len(outline.beats) == 8

    @pytest.mark.asyncio
    async def test_falls_back_to_zh_tw_for_unknown_language(self) -> None:
        model = _FakeModel("not json at all")
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(
            prompt="提示", briefs=_briefs(),
            operator_primary_language="fr-FR",
        )
        assert _has_han(_outline_blob(outline))

    @pytest.mark.asyncio
    async def test_english_beats_preserve_act_monotone_order(self) -> None:
        model = _FakeModel("not json at all")
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(
            prompt="a prompt", briefs=_briefs(),
            operator_primary_language="en-US",
        )
        assert [b.act for b in outline.beats] == [
            ACT_OPENING,
            ACT_RISING, ACT_RISING, ACT_RISING, ACT_RISING,
            ACT_TURN, ACT_TURN,
            ACT_RESOLUTION,
        ]

    @pytest.mark.asyncio
    async def test_blank_prompt_title_localizes_english(self) -> None:
        # Fallback title defaults to "未命名的相遇" when prompt is blank —
        # this default must also localize.
        model = _FakeModel("not json at all")
        planner = FusionStoryPlanner(model=model)
        outline = await planner.plan(
            prompt="", briefs=_briefs(),
            operator_primary_language="en-US",
        )
        assert not _has_han(outline.title), outline.title
