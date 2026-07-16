"""Polish stage of the fusion-story pipeline.

Two distinct rewrite paths:

- ``polish_whole`` — single LLM call that rewrites the whole draft.
  Used for the global polish (severity = SEVERE, or critique findings
  are all whole-story / index-less observations).
- ``polish_spots`` — one LLM call per flagged paragraph, each scoped to
  the target paragraph plus 1 paragraph of context on either side. The
  rest of the draft is passed through verbatim. This keeps token cost
  bounded regardless of how long the story gets, and stops the
  polisher from drifting on paragraphs that the critic didn't flag.

Public ``polish()`` dispatches between them based on the critique.
Falls back to returning ``draft_text`` unchanged when the LLM call
fails or returns empty so the loop terminates gracefully.
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
from kokoro_link.domain.value_objects.fusion_critique import (
    FusionCritiqueFinding,
    FusionStoryCritique,
    SEVERITY_SEVERE,
)
from kokoro_link.domain.value_objects.fusion_outline import FusionOutline
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader


_LOGGER = logging.getLogger(__name__)

# Range scales with the planner's 6–10 beat shape (~700–1000 字/幕).
_TARGET_MIN = 5000
_TARGET_MAX = 8500

_SPOT_CONTEXT_RADIUS = 1
"""How many paragraphs of surrounding context to show the LLM when
rewriting a single paragraph. 1 = previous + next; usually enough to
maintain 銜接 without dragging the whole draft into the prompt."""


class FusionStoryPolisher:
    """LLM-backed polish pass — dispatches to whole or spot rewrite."""

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
        prompt: str,
        outline: FusionOutline,
        draft_text: str,
        briefs: Sequence[CharacterBrief],
        critique: FusionStoryCritique | None = None,
        round_index: int = 0,
        operator_primary_language: str = "zh-TW",
    ) -> str:
        """Pick whole vs. spot mode based on the critique, then run it.

        - No critique or no findings → whole polish (generic smoothing).
        - severity == SEVERE → whole polish (structural rewrite needed).
        - At least one finding has a paragraph anchor → spot polish;
          findings without an anchor are folded into the prompt as
          context-aware notes for any spot rewrite that fires.
        - All findings are anchorless → whole polish (cross-paragraph).
        """
        draft_text = (draft_text or "").strip()
        if not draft_text:
            return draft_text
        if await self._resolver.is_fake():
            return draft_text

        if critique is None or not critique.findings:
            return await self.polish_whole(
                prompt=prompt, outline=outline, draft_text=draft_text,
                briefs=briefs, critique=critique, round_index=round_index,
                operator_primary_language=operator_primary_language,
            )
        if critique.severity >= SEVERITY_SEVERE:
            return await self.polish_whole(
                prompt=prompt, outline=outline, draft_text=draft_text,
                briefs=briefs, critique=critique, round_index=round_index,
                operator_primary_language=operator_primary_language,
            )
        anchored = [f for f in critique.findings if f.has_anchor()]
        if not anchored:
            return await self.polish_whole(
                prompt=prompt, outline=outline, draft_text=draft_text,
                briefs=briefs, critique=critique, round_index=round_index,
                operator_primary_language=operator_primary_language,
            )
        return await self.polish_spots(
            prompt=prompt, outline=outline, draft_text=draft_text,
            briefs=briefs, critique=critique, round_index=round_index,
            operator_primary_language=operator_primary_language,
        )

    async def polish_whole(
        self,
        *,
        prompt: str,
        outline: FusionOutline,
        draft_text: str,
        briefs: Sequence[CharacterBrief],
        critique: FusionStoryCritique | None = None,
        round_index: int = 0,
        operator_primary_language: str = "zh-TW",
    ) -> str:
        """Whole-draft rewrite in one LLM call. Falls back to the input
        on empty output / exception."""
        draft_text = (draft_text or "").strip()
        if not draft_text:
            return draft_text

        full_prompt = _build_whole_prompt(
            prompt=prompt,
            outline=outline,
            draft_text=draft_text,
            briefs=briefs,
            critique=critique,
            round_index=round_index,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(full_prompt)
        except Exception:
            _LOGGER.exception("fusion polisher (whole) LLM call failed")
            return draft_text
        cleaned = raw.strip()
        if not cleaned:
            _LOGGER.warning("fusion polisher (whole): empty output")
            return draft_text
        return cleaned

    async def polish_spots(
        self,
        *,
        prompt: str,
        outline: FusionOutline,
        draft_text: str,
        briefs: Sequence[CharacterBrief],
        critique: FusionStoryCritique,
        round_index: int = 0,
        operator_primary_language: str = "zh-TW",
    ) -> str:
        """Rewrite only the paragraphs the critic anchored a finding to.

        One LLM call per paragraph (deduplicated). Anchorless findings
        ride along as additional context so the spot LLM is aware of
        cross-paragraph concerns when it touches its assigned span.
        """
        paragraphs = _split_paragraphs(draft_text)
        if not paragraphs:
            return draft_text

        # Group findings by paragraph_index. Anchorless findings become
        # ambient notes attached to every spot call so the LLM doesn't
        # produce a paragraph that locally fixes one thing but
        # re-introduces a global issue.
        per_paragraph: dict[int, list[FusionCritiqueFinding]] = {}
        ambient: list[FusionCritiqueFinding] = []
        for finding in critique.findings:
            if finding.paragraph_index is None:
                ambient.append(finding)
                continue
            if 0 <= finding.paragraph_index < len(paragraphs):
                per_paragraph.setdefault(
                    finding.paragraph_index, [],
                ).append(finding)

        if not per_paragraph:
            # All anchors were out of range — fall back to whole rewrite
            # rather than silently doing nothing.
            return await self.polish_whole(
                prompt=prompt, outline=outline, draft_text=draft_text,
                briefs=briefs, critique=critique, round_index=round_index,
                operator_primary_language=operator_primary_language,
            )

        cast = "、".join(b.short_label() for b in briefs) or "（未指定）"
        for idx in sorted(per_paragraph.keys()):
            target = paragraphs[idx]
            context_before = _join_window(
                paragraphs, idx - _SPOT_CONTEXT_RADIUS, idx,
            )
            context_after = _join_window(
                paragraphs, idx + 1, idx + 1 + _SPOT_CONTEXT_RADIUS,
            )
            spot_prompt = _build_spot_prompt(
                prompt=prompt,
                outline=outline,
                cast=cast,
                target_index=idx,
                target_text=target,
                context_before=context_before,
                context_after=context_after,
                findings=per_paragraph[idx],
                ambient_findings=ambient,
                round_index=round_index,
                operator_primary_language=operator_primary_language,
            )
            try:
                raw = await self._resolver.generate(spot_prompt)
            except Exception:
                _LOGGER.exception(
                    "fusion polisher (spot) LLM call failed idx=%s", idx,
                )
                continue
            rewritten = raw.strip()
            if not rewritten:
                _LOGGER.warning(
                    "fusion polisher (spot): empty output idx=%s", idx,
                )
                continue
            paragraphs[idx] = rewritten
        return "\n\n".join(paragraphs)


# --- prompt builders -------------------------------------------------


def _build_whole_prompt(
    *,
    prompt: str,
    outline: FusionOutline,
    draft_text: str,
    briefs: Sequence[CharacterBrief],
    critique: FusionStoryCritique | None,
    round_index: int,
    operator_primary_language: str = "zh-TW",
) -> str:
    cast = "、".join(b.short_label() for b in briefs) or "（未指定）"
    transition_block = _render_transition_block(outline)
    critique_block = _render_critique_block(critique)
    body = get_default_loader().render(
        "fusion/polisher_whole",
        round_number=round_index + 1,
        theme=outline.theme,
        title=outline.title,
        cast=cast,
        prompt_text=prompt.strip() or "（未指定）",
        transition_block=transition_block,
        draft_text=draft_text,
        critique_block=critique_block,
        target_min=_TARGET_MIN,
        target_max=_TARGET_MAX,
    )
    language_hint = render_operator_language_hint(operator_primary_language)
    return f"{language_hint}\n\n{body}" if language_hint else body


def _build_spot_prompt(
    *,
    prompt: str,
    outline: FusionOutline,
    cast: str,
    target_index: int,
    target_text: str,
    context_before: str,
    context_after: str,
    findings: Sequence[FusionCritiqueFinding],
    ambient_findings: Sequence[FusionCritiqueFinding],
    round_index: int,
    operator_primary_language: str = "zh-TW",
) -> str:
    issue_block = _render_findings_block(
        findings, header="critic 對這一段的具體點評（必須逐條解決）：",
    )
    ambient_block = (
        _render_findings_block(
            ambient_findings,
            header="critic 對全篇的整體提醒（在改寫這段時也要避免重蹈）：",
        )
        if ambient_findings
        else ""
    )
    before_block = (
        f"上文（不要重寫、僅供參考銜接）：\n<<<\n{context_before}\n>>>\n"
        if context_before
        else "（這是第一段，沒有上文）\n"
    )
    after_block = (
        f"下文（不要重寫、僅供參考銜接）：\n<<<\n{context_after}\n>>>\n"
        if context_after
        else "（這是最後一段，沒有下文）\n"
    )
    body = get_default_loader().render(
        "fusion/polisher_spot",
        round_number=round_index + 1,
        theme=outline.theme,
        title=outline.title,
        cast=cast,
        prompt_text=prompt.strip() or "（未指定）",
        target_index=target_index,
        target_text=target_text,
        before_block=before_block,
        after_block=after_block,
        issue_block=issue_block,
        ambient_block=ambient_block,
    )
    language_hint = render_operator_language_hint(operator_primary_language)
    return f"{language_hint}\n\n{body}" if language_hint else body


def _render_transition_block(outline: FusionOutline) -> str:
    """Echo the outline transition contract so polish can rewrite any
    bridge that drifted from the spec."""
    lines: list[str] = []
    for b in outline.beats:
        bits: list[str] = [f"第 {b.sequence + 1} 幕「{b.title}」"]
        if b.transition_from_previous and b.sequence > 0:
            bits.append(f"銜接：{b.transition_from_previous}")
        if b.entry_state:
            bits.append(f"開場：{b.entry_state}")
        if b.exit_state:
            bits.append(f"結束：{b.exit_state}")
        if len(bits) > 1:
            lines.append(" ｜ ".join(bits))
    if not lines:
        return ""
    return (
        "幕間轉場規劃（成品若與此不符，優先按規劃改寫）：\n"
        + "\n".join(lines) + "\n"
    )


def _render_critique_block(
    critique: FusionStoryCritique | None,
) -> str:
    """Render the critic's findings as a numbered fix list for the
    whole-rewrite prompt. Empty when no critique or critique is clean.
    """
    if critique is None or not critique.findings:
        return ""
    header = "上一輪 critic 點出下列問題，**這輪必須逐條解決**："
    summary_line = (
        f"  critic 總評：{critique.summary}\n" if critique.summary else ""
    )
    items = _render_findings_items(critique.findings)
    return header + "\n" + summary_line + items + "\n"


def _render_findings_block(
    findings: Sequence[FusionCritiqueFinding], *, header: str,
) -> str:
    if not findings:
        return ""
    return header + "\n" + _render_findings_items(findings) + "\n"


def _render_findings_items(
    findings: Sequence[FusionCritiqueFinding],
) -> str:
    parts: list[str] = []
    for i, f in enumerate(findings, start=1):
        anchor = (
            f" @[#{f.paragraph_index}]"
            if f.paragraph_index is not None
            else " (跨段落)"
        )
        line = [f"  {i}. [{f.kind}]{anchor} {f.issue}"]
        if f.quote:
            line.append(f"     引文：「{f.quote}」")
        if f.suggestion:
            line.append(f"     方向：{f.suggestion}")
        parts.append("\n".join(line))
    return "\n".join(parts)


def _split_paragraphs(draft_text: str) -> list[str]:
    """Match the critic's splitter so paragraph indices stay aligned."""
    parts = [p.strip() for p in (draft_text or "").split("\n\n")]
    return [p for p in parts if p]


def _join_window(
    paragraphs: Sequence[str], start: int, end: int,
) -> str:
    """Clamp [start, end) to the paragraph list and join with blank lines."""
    lo = max(0, start)
    hi = min(len(paragraphs), end)
    if lo >= hi:
        return ""
    return "\n\n".join(paragraphs[lo:hi])
