"""LLM-backed polisher for branching-drama narration.

Whole-text rewrite only — drama narrations are short (300–500 字), so
the spot-polish optimisation that fusion uses (one LLM call per
flagged paragraph) doesn't earn its complexity here. One critic + one
whole rewrite is plenty.

Falls back to the original draft on empty output / LLM error so the
gameplay path never breaks because of a bad polish.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.branching_drama import (
    DramaNode,
    DramaSessionTurn,
)
from kokoro_link.domain.value_objects.drama_critique import (
    DramaCritique,
    DramaCritiqueFinding,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader


_LOGGER = logging.getLogger(__name__)
_PRIOR_TURN_SNIPPET = 220
_PRIOR_TURN_LIMIT = 5


class BranchingDramaPolisher:
    """LLM-backed rewriter that takes a critic verdict and returns a
    repaired narration. Returns the input on no-op / failure."""

    def __init__(
        self,
        *,
        provider: ActiveLLMProviderPort | None = None,
        model: ChatModelPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def polish(
        self,
        *,
        node: DramaNode,
        narration_text: str,
        critique: DramaCritique,
        briefs: Sequence[CharacterBrief],
        previous_turns: Sequence[DramaSessionTurn] = (),
        operator_primary_language: str = "zh-TW",
    ) -> str:
        narration_text = (narration_text or "").strip()
        if not narration_text:
            return narration_text
        if not critique.has_issues():
            return narration_text
        if await self._resolver.is_fake():
            return narration_text

        full_prompt = _build_prompt(
            node=node,
            narration_text=narration_text,
            critique=critique,
            briefs=briefs,
            previous_turns=previous_turns,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(full_prompt)
        except Exception:
            _LOGGER.exception("drama polisher LLM call failed")
            return narration_text
        cleaned = (raw or "").strip()
        if not cleaned:
            _LOGGER.warning("drama polisher: empty output")
            return narration_text
        return cleaned


def _summarise_prior_turns(turns: Sequence[DramaSessionTurn]) -> str:
    if not turns:
        return "（這是第一幕，沒有前情）"
    selected = list(turns)[-_PRIOR_TURN_LIMIT:]
    lines: list[str] = []
    for idx, turn in enumerate(selected, start=1):
        snippet = turn.narration.strip().replace("\n", " ")
        if len(snippet) > _PRIOR_TURN_SNIPPET:
            snippet = snippet[:_PRIOR_TURN_SNIPPET] + "…"
        tone_label = turn.chosen_tone or "（無 tone）"
        lines.append(f"[幕 {idx}｜{tone_label}] {snippet}")
    return "\n".join(lines)


def _render_findings(findings: tuple[DramaCritiqueFinding, ...]) -> str:
    if not findings:
        return "（critic 未列出具體 finding，請依語感整體潤一輪）"
    lines: list[str] = []
    for i, f in enumerate(findings, start=1):
        anchor = (
            f"段 #{f.paragraph_index}" if f.paragraph_index is not None
            else "整段"
        )
        quote_line = f"\n  原文：「{f.quote}」" if f.quote else ""
        suggestion_line = (
            f"\n  建議方向：{f.suggestion}" if f.suggestion else ""
        )
        lines.append(
            f"{i}. [{f.kind}｜{anchor}] {f.issue}"
            f"{quote_line}{suggestion_line}"
        )
    return "\n".join(lines)


def _build_prompt(
    *,
    node: DramaNode,
    narration_text: str,
    critique: DramaCritique,
    briefs: Sequence[CharacterBrief],
    previous_turns: Sequence[DramaSessionTurn],
    operator_primary_language: str = "zh-TW",
) -> str:
    cast = "、".join(b.short_label() for b in briefs) or "（未指定）"
    brief_block = "\n\n".join(b.text for b in briefs) or "（無）"
    findings_block = _render_findings(critique.findings)
    prior_block = _summarise_prior_turns(previous_turns)
    tone_line = (
        f"本段取向：{node.tone}" if node.tone else "本段取向：（未指定）"
    )

    body = get_default_loader().render(
        "branching/polisher",
        brief_block=brief_block,
        node_title=node.title,
        node_summary=node.summary,
        tone_line=tone_line,
        cast=cast,
        prior_block=prior_block,
        narration_text=narration_text,
        critique_summary=critique.summary or "（未提供）",
        findings_block=findings_block,
    )
    language_hint = render_operator_language_hint(operator_primary_language)
    return f"{language_hint}\n\n{body}" if language_hint else body
