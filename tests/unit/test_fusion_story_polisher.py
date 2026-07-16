"""FusionStoryPolisher dispatch + spot-rewrite tests.

The polisher has two LLM paths — whole-draft rewrite and per-paragraph
spot rewrite — and a dispatcher that picks between them based on the
critique. These tests pin:

- Dispatcher routing: no critique → whole; SEVERE → whole; only
  anchorless findings → whole; at least one anchored finding → spot.
- Spot mode emits exactly one LLM call per anchored paragraph and the
  resulting draft preserves untouched paragraphs verbatim.
- Out-of-range anchors fall back to a whole rewrite rather than
  silently dropping the findings.
- LLM failures (exception / empty) leave the input unchanged so the
  outer critic→polish loop terminates gracefully.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
)
from kokoro_link.application.services.fusion_story_polisher import (
    FusionStoryPolisher,
)
from kokoro_link.domain.value_objects.fusion_critique import (
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


class _ScriptedModel:
    """Records every prompt sent and returns a scripted response.

    The response template uses `{n}` where `n` is the call index so the
    polisher's output is distinguishable per LLM call — that's how the
    spot tests verify which paragraph got rewritten.
    """

    supports_vision = False
    provider_id = "scripted"

    def __init__(self, template: str = "REWRITTEN[{n}]") -> None:
        self._template = template
        self.prompts: list[str] = []

    async def generate(
        self, prompt: str, *, image_urls=None, model=None,  # noqa: ARG002
    ) -> str:
        self.prompts.append(prompt)
        return self._template.format(n=len(self.prompts) - 1)

    def generate_stream(
        self, prompt: str, *, image_urls=None, model=None,  # noqa: ARG002
    ):
        async def _empty():
            if False:
                yield ""
        return _empty()

    async def list_models(self) -> list[str]:
        return []


class _BoomModel(_ScriptedModel):
    async def generate(
        self, prompt: str, *, image_urls=None, model=None,  # noqa: ARG002
    ) -> str:
        self.prompts.append(prompt)
        raise RuntimeError("LLM exploded")


class _EmptyModel(_ScriptedModel):
    async def generate(
        self, prompt: str, *, image_urls=None, model=None,  # noqa: ARG002
    ) -> str:
        self.prompts.append(prompt)
        return "   "


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


def _draft() -> str:
    return (
        "第零段的內容。\n\n"
        "第一段的內容。\n\n"
        "第二段的內容。\n\n"
        "第三段的內容。"
    )


def _anchored_critique(paragraph_index: int) -> FusionStoryCritique:
    return FusionStoryCritique.create(
        severity=SEVERITY_MAJOR,
        summary="single anchor",
        findings=[FusionCritiqueFinding.create(
            kind="抽象",
            paragraph_index=paragraph_index,
            issue="抽象問題",
        )],
        should_continue=True,
    )


class TestPolisherDispatch:
    @pytest.mark.asyncio
    async def test_anchored_finding_triggers_spot_polish(self) -> None:
        # One anchor on paragraph #2 → exactly one LLM call, only that
        # paragraph is rewritten; the rest of the draft passes through.
        model = _ScriptedModel()
        polisher = FusionStoryPolisher(model=model)
        out = await polisher.polish(
            prompt="p", outline=_outline(),
            draft_text=_draft(), briefs=_briefs(),
            critique=_anchored_critique(2),
        )
        assert len(model.prompts) == 1
        # Spot prompt cites the target index explicitly.
        assert "[#2]" in model.prompts[0]
        # Output: 0, 1, REWRITTEN[0], 3 — only index 2 was replaced.
        paragraphs = out.split("\n\n")
        assert paragraphs[0] == "第零段的內容。"
        assert paragraphs[1] == "第一段的內容。"
        assert paragraphs[2] == "REWRITTEN[0]"
        assert paragraphs[3] == "第三段的內容。"

    @pytest.mark.asyncio
    async def test_multiple_anchors_emit_one_call_per_paragraph(self) -> None:
        critique = FusionStoryCritique.create(
            severity=SEVERITY_MAJOR,
            findings=[
                FusionCritiqueFinding.create(
                    kind="抽象", paragraph_index=0, issue="x",
                ),
                FusionCritiqueFinding.create(
                    kind="重複", paragraph_index=2, issue="y",
                ),
            ],
            should_continue=True,
        )
        model = _ScriptedModel()
        polisher = FusionStoryPolisher(model=model)
        out = await polisher.polish(
            prompt="p", outline=_outline(),
            draft_text=_draft(), briefs=_briefs(),
            critique=critique,
        )
        # Two anchors → two LLM calls, sorted by index.
        assert len(model.prompts) == 2
        paragraphs = out.split("\n\n")
        assert paragraphs[0].startswith("REWRITTEN[")
        assert paragraphs[1] == "第一段的內容。"
        assert paragraphs[2].startswith("REWRITTEN[")
        assert paragraphs[3] == "第三段的內容。"

    @pytest.mark.asyncio
    async def test_severe_severity_forces_whole_polish(self) -> None:
        # Even with anchors, SEVERE means cross-paragraph structural
        # rewrite is needed — dispatcher must escalate to whole mode.
        critique = FusionStoryCritique.create(
            severity=SEVERITY_SEVERE,
            findings=[FusionCritiqueFinding.create(
                kind="節奏", paragraph_index=1,
                issue="整體節奏崩了",
            )],
            should_continue=True,
        )
        model = _ScriptedModel(template="WHOLE_REWRITE")
        polisher = FusionStoryPolisher(model=model)
        out = await polisher.polish(
            prompt="p", outline=_outline(),
            draft_text=_draft(), briefs=_briefs(),
            critique=critique,
        )
        assert len(model.prompts) == 1
        # Whole prompt mentions the full-draft rewrite, not [#N].
        assert "整篇潤稿" in model.prompts[0]
        assert out == "WHOLE_REWRITE"

    @pytest.mark.asyncio
    async def test_only_anchorless_findings_go_whole(self) -> None:
        critique = FusionStoryCritique.create(
            severity=SEVERITY_MAJOR,
            findings=[FusionCritiqueFinding.create(
                kind="節奏", issue="跨段落節奏問題",
            )],
            should_continue=True,
        )
        model = _ScriptedModel(template="WHOLE_OUT")
        polisher = FusionStoryPolisher(model=model)
        out = await polisher.polish(
            prompt="p", outline=_outline(),
            draft_text=_draft(), briefs=_briefs(),
            critique=critique,
        )
        assert len(model.prompts) == 1
        assert out == "WHOLE_OUT"

    @pytest.mark.asyncio
    async def test_anchorless_finding_rides_along_with_spot_call(self) -> None:
        # When a critique mixes anchored + anchorless findings, the spot
        # call should still surface the ambient one as context so the
        # rewrite avoids re-introducing global problems.
        critique = FusionStoryCritique.create(
            severity=SEVERITY_MAJOR,
            findings=[
                FusionCritiqueFinding.create(
                    kind="抽象", paragraph_index=1, issue="此段太抽象",
                ),
                FusionCritiqueFinding.create(
                    kind="節奏", issue="GLOBAL_AMBIENT_NOTE",
                ),
            ],
            should_continue=True,
        )
        model = _ScriptedModel()
        polisher = FusionStoryPolisher(model=model)
        await polisher.polish(
            prompt="p", outline=_outline(),
            draft_text=_draft(), briefs=_briefs(),
            critique=critique,
        )
        assert len(model.prompts) == 1
        assert "GLOBAL_AMBIENT_NOTE" in model.prompts[0]

    @pytest.mark.asyncio
    async def test_out_of_range_anchor_falls_back_to_whole(self) -> None:
        # If the only finding's anchor is out of range (e.g. an older
        # critique used against a freshly polished draft with fewer
        # paragraphs), spot mode has nothing to do — escalate to whole
        # rather than silently no-op.
        critique = FusionStoryCritique.create(
            severity=SEVERITY_MAJOR,
            findings=[FusionCritiqueFinding.create(
                kind="抽象", paragraph_index=99, issue="phantom",
            )],
            should_continue=True,
        )
        model = _ScriptedModel(template="WHOLE_FALLBACK")
        polisher = FusionStoryPolisher(model=model)
        out = await polisher.polish(
            prompt="p", outline=_outline(),
            draft_text=_draft(), briefs=_briefs(),
            critique=critique,
        )
        assert out == "WHOLE_FALLBACK"


class TestPolisherFailureModes:
    @pytest.mark.asyncio
    async def test_spot_llm_exception_keeps_paragraph_unchanged(self) -> None:
        # If the spot LLM call blows up, the paragraph stays as-is so the
        # outer critic→polish loop can still terminate the round.
        model = _BoomModel()
        polisher = FusionStoryPolisher(model=model)
        out = await polisher.polish(
            prompt="p", outline=_outline(),
            draft_text=_draft(), briefs=_briefs(),
            critique=_anchored_critique(1),
        )
        # Output is identical to input draft — paragraph 1 unchanged.
        assert out == _draft()

    @pytest.mark.asyncio
    async def test_spot_empty_output_keeps_paragraph_unchanged(self) -> None:
        model = _EmptyModel()
        polisher = FusionStoryPolisher(model=model)
        out = await polisher.polish(
            prompt="p", outline=_outline(),
            draft_text=_draft(), briefs=_briefs(),
            critique=_anchored_critique(1),
        )
        assert out == _draft()

    @pytest.mark.asyncio
    async def test_whole_llm_exception_returns_input(self) -> None:
        model = _BoomModel()
        polisher = FusionStoryPolisher(model=model)
        out = await polisher.polish(
            prompt="p", outline=_outline(),
            draft_text=_draft(), briefs=_briefs(),
            critique=None,  # whole-mode path
        )
        assert out == _draft()
