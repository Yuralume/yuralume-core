"""FusionStoryCritic parser + safe-fallback tests.

The critic is an LLM call wrapped in a parser. Tests cover:

- Happy path: structured JSON parses into a ``FusionStoryCritique``.
- Defensive: code-fence wrapping, severity coercion, partial findings.
- Safety: fake provider / blank input / unparseable output all return a
  CLEAN verdict so the polish loop can terminate gracefully.
"""

from __future__ import annotations

import json

import pytest

from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
)
from kokoro_link.application.services.fusion_story_critic import (
    FusionStoryCritic,
)
from kokoro_link.domain.value_objects.fusion_critique import (
    SEVERITY_CLEAN,
    SEVERITY_MAJOR,
    SEVERITY_SEVERE,
    FusionCritiqueFinding,
    FusionStoryCritique,
)
from kokoro_link.domain.value_objects.fusion_outline import (
    ACT_OPENING,
    ACT_RESOLUTION,
    ACT_RISING,
    ACT_TURN,
    FusionBeatPlan,
    FusionOutline,
)


class _FakeModel:
    """Minimal ``ChatModelPort`` stand-in. Echoes a scripted response."""

    supports_vision = False
    provider_id = "fake"

    def __init__(self, response: str) -> None:
        self._response = response
        self.last_prompt: str | None = None

    async def generate(
        self, prompt: str, *, image_urls=None, model=None,  # noqa: ARG002
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


class _BoomModel(_FakeModel):
    """LLM that always raises so we can pin the exception-fallback path."""

    async def generate(
        self, prompt: str, *, image_urls=None, model=None,  # noqa: ARG002
    ) -> str:
        raise RuntimeError("LLM exploded")


def _outline() -> FusionOutline:
    beats = [
        FusionBeatPlan.create(
            sequence=i, act=act, title=f"幕{i}",
            hook=f"hook{i}", dramatic_question="",
            target_chars=500, focus_character_ids=("a", "b"),
        )
        for i, act in enumerate(
            (ACT_OPENING, ACT_RISING, ACT_TURN, ACT_RESOLUTION),
        )
    ]
    return FusionOutline.create(
        title="標題", premise="前提", theme="custom", beats=beats,
    )


def _briefs() -> list[CharacterBrief]:
    return [
        CharacterBrief(
            character_id="a", name="A", summary="x",
            text="## 角色：A (id=a)",
        ),
        CharacterBrief(
            character_id="b", name="B", summary="y",
            text="## 角色：B (id=b)",
        ),
    ]


def _ok_response() -> str:
    return json.dumps(
        {
            "severity": 2,
            "summary": "第三段全是形容詞，建議具體化",
            "should_continue": True,
            "findings": [
                {
                    "kind": "抽象",
                    "paragraph_index": 1,
                    "quote": "她感到憂傷，氣氛變得微妙",
                    "issue": "沒有任何感官支撐",
                    "suggestion": "用一個動作或物件取代心情敘述",
                },
                {
                    "kind": "重複",
                    "paragraph_index": 2,
                    "quote": "他看向窗外",
                    "issue": "同一句在第二與第四幕都出現",
                    "suggestion": "改寫其中一處的視角錨點",
                },
            ],
        },
        ensure_ascii=False,
    )


# A multi-paragraph draft so paragraph_index assertions land in-range.
_DRAFT = "第一段內容。\n\n第二段內容。\n\n第三段內容。\n\n第四段內容。"


class TestCriticHappyPath:
    @pytest.mark.asyncio
    async def test_parses_structured_verdict(self) -> None:
        model = _FakeModel(_ok_response())
        critic = FusionStoryCritic(model=model)
        verdict = await critic.review(
            prompt="提示",
            outline=_outline(),
            draft_text=_DRAFT,
            briefs=_briefs(),
        )
        assert verdict.severity == SEVERITY_MAJOR
        assert verdict.should_continue is True
        assert len(verdict.findings) == 2
        assert verdict.findings[0].kind == "抽象"
        assert verdict.findings[0].paragraph_index == 1
        assert verdict.findings[0].suggestion.startswith("用一個動作")
        assert verdict.findings[1].paragraph_index == 2

    @pytest.mark.asyncio
    async def test_strips_code_fence(self) -> None:
        wrapped = "```json\n" + _ok_response() + "\n```"
        model = _FakeModel(wrapped)
        critic = FusionStoryCritic(model=model)
        verdict = await critic.review(
            prompt="p", outline=_outline(),
            draft_text=_DRAFT, briefs=_briefs(),
        )
        assert verdict.severity == SEVERITY_MAJOR
        assert len(verdict.findings) == 2

    @pytest.mark.asyncio
    async def test_prompt_enumerates_paragraphs(self) -> None:
        # Paragraph indices in the prompt are how the critic anchors
        # findings; the polisher relies on the same enumeration to do
        # spot rewrites. Pin the rendering so the contract doesn't drift.
        model = _FakeModel(_ok_response())
        critic = FusionStoryCritic(model=model)
        await critic.review(
            prompt="p", outline=_outline(),
            draft_text=_DRAFT, briefs=_briefs(),
        )
        assert model.last_prompt is not None
        assert "[#0] 第一段內容。" in model.last_prompt
        assert "[#3] 第四段內容。" in model.last_prompt

    @pytest.mark.asyncio
    async def test_previous_critique_appears_in_prompt(self) -> None:
        # When the previous round's findings are handed in, the prompt
        # must echo them so the LLM can spot "polisher didn't fix it".
        prior = FusionStoryCritique.create(
            severity=SEVERITY_MAJOR,
            findings=[FusionCritiqueFinding.create(
                kind="抽象", issue="第三段全是形容詞",
                quote="她感到憂傷",
            )],
            should_continue=True,
        )
        model = _FakeModel(_ok_response())
        critic = FusionStoryCritic(model=model)
        await critic.review(
            prompt="p", outline=_outline(),
            draft_text=_DRAFT, briefs=_briefs(),
            round_index=1, previous_critique=prior,
        )
        assert model.last_prompt is not None
        assert "她感到憂傷" in model.last_prompt
        assert "polisher 未處理" in model.last_prompt


class TestCriticDefensiveParsing:
    @pytest.mark.asyncio
    async def test_severity_clamps_into_valid_range(self) -> None:
        payload = json.dumps({
            "severity": 99,
            "summary": "out of range",
            "should_continue": True,
            "findings": [
                {"kind": "重複", "issue": "issue"},
            ],
        })
        model = _FakeModel(payload)
        critic = FusionStoryCritic(model=model)
        verdict = await critic.review(
            prompt="p", outline=_outline(),
            draft_text=_DRAFT, briefs=_briefs(),
        )
        assert verdict.severity == SEVERITY_SEVERE

    @pytest.mark.asyncio
    async def test_findings_without_kind_or_issue_are_dropped(self) -> None:
        payload = json.dumps({
            "severity": 2,
            "summary": "",
            "should_continue": True,
            "findings": [
                {"kind": "", "issue": "missing kind"},
                {"kind": "ok", "issue": ""},
                {"kind": "ok", "issue": "real"},
            ],
        })
        model = _FakeModel(payload)
        critic = FusionStoryCritic(model=model)
        verdict = await critic.review(
            prompt="p", outline=_outline(),
            draft_text=_DRAFT, briefs=_briefs(),
        )
        # Only the third finding survives.
        assert len(verdict.findings) == 1
        assert verdict.findings[0].issue == "real"

    @pytest.mark.asyncio
    async def test_should_continue_defaults_from_severity(self) -> None:
        # Omitting should_continue → derive from severity. CLEAN means
        # stop; >0 means keep going.
        payload = json.dumps({
            "severity": 0,
            "summary": "fine",
            "findings": [],
        })
        model = _FakeModel(payload)
        critic = FusionStoryCritic(model=model)
        verdict = await critic.review(
            prompt="p", outline=_outline(),
            draft_text=_DRAFT, briefs=_briefs(),
        )
        assert verdict.severity == SEVERITY_CLEAN
        assert verdict.should_continue is False

    @pytest.mark.asyncio
    async def test_paragraph_index_out_of_range_becomes_none(self) -> None:
        # _DRAFT has 4 paragraphs (indices 0–3). An LLM that emits 99
        # is pointing at nothing; we must coerce to None rather than
        # anchor the polisher on a phantom paragraph.
        payload = json.dumps({
            "severity": 2,
            "summary": "out-of-range index",
            "should_continue": True,
            "findings": [
                {
                    "kind": "抽象",
                    "paragraph_index": 99,
                    "issue": "pointer is bogus",
                },
                {
                    "kind": "抽象",
                    "paragraph_index": -1,
                    "issue": "negative is also bogus",
                },
                {
                    "kind": "抽象",
                    "paragraph_index": "garbage",
                    "issue": "junk string",
                },
                {
                    "kind": "節奏",
                    "paragraph_index": None,
                    "issue": "legitimate cross-paragraph note",
                },
            ],
        })
        model = _FakeModel(payload)
        critic = FusionStoryCritic(model=model)
        verdict = await critic.review(
            prompt="p", outline=_outline(),
            draft_text=_DRAFT, briefs=_briefs(),
        )
        assert len(verdict.findings) == 4
        assert all(
            f.paragraph_index is None for f in verdict.findings
        )

    @pytest.mark.asyncio
    async def test_paragraph_index_in_range_is_preserved(self) -> None:
        payload = json.dumps({
            "severity": 2,
            "summary": "",
            "should_continue": True,
            "findings": [
                {
                    "kind": "抽象",
                    "paragraph_index": 0,
                    "issue": "first paragraph",
                },
                {
                    "kind": "重複",
                    "paragraph_index": 3,
                    "issue": "last paragraph",
                },
            ],
        })
        model = _FakeModel(payload)
        critic = FusionStoryCritic(model=model)
        verdict = await critic.review(
            prompt="p", outline=_outline(),
            draft_text=_DRAFT, briefs=_briefs(),
        )
        assert [f.paragraph_index for f in verdict.findings] == [0, 3]


class TestCriticSafeFallbacks:
    @pytest.mark.asyncio
    async def test_blank_draft_returns_clean(self) -> None:
        # No prose → nothing to critique. The loop should stop.
        model = _FakeModel("anything")
        critic = FusionStoryCritic(model=model)
        verdict = await critic.review(
            prompt="p", outline=_outline(),
            draft_text="   \n  ", briefs=_briefs(),
        )
        assert verdict.severity == SEVERITY_CLEAN
        # The model should NOT have been called.
        assert model.last_prompt is None

    @pytest.mark.asyncio
    async def test_unparseable_output_returns_clean(self) -> None:
        model = _FakeModel("totally not json")
        critic = FusionStoryCritic(model=model)
        verdict = await critic.review(
            prompt="p", outline=_outline(),
            draft_text="some prose", briefs=_briefs(),
        )
        # Fallback CLEAN so the orchestrator can terminate.
        assert verdict.severity == SEVERITY_CLEAN

    @pytest.mark.asyncio
    async def test_llm_exception_returns_clean(self) -> None:
        critic = FusionStoryCritic(model=_BoomModel(""))
        verdict = await critic.review(
            prompt="p", outline=_outline(),
            draft_text="some prose", briefs=_briefs(),
        )
        assert verdict.severity == SEVERITY_CLEAN
