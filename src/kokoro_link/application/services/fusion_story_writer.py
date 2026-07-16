"""Per-beat writer stage of the fusion-story pipeline.

For each beat in the outline we run a standalone LLM call:

    (prompt, briefs, outline so-far summary, this beat plan) → prose

The writer is intentionally stateless about the outer story — the
orchestrator hands it the beat plus a "previously" summary built from
already-written beats so context flows forward without paying for the
prior prose in every call.

Falling back to a synthetic paragraph keeps the pipeline from stalling
when the LLM errors out on a single beat — operators can re-run that
beat manually via ``iterate/beat/{idx}`` once the provider is healthy.
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
from kokoro_link.domain.value_objects.fusion_outline import (
    FusionBeatPlan,
    FusionOutline,
)
from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
)
from kokoro_link.infrastructure.prompts import get_default_loader


_LOGGER = logging.getLogger(__name__)


class FusionStoryWriter:
    """LLM-backed per-beat prose writer."""

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

    async def write_beat(
        self,
        *,
        prompt: str,
        outline: FusionOutline,
        beat: FusionBeatPlan,
        briefs: Sequence[CharacterBrief],
        previously_summary: str = "",
        previous_tail: str = "",
        regenerate_hint: str | None = None,
        operator_primary_language: str = "zh-TW",
    ) -> str:
        """Return prose for ``beat``. Never raises — falls back on error.

        ``previously_summary`` is the structural recap (act / hook /
        short preview per prior beat). ``previous_tail`` is the **raw
        last paragraph(s)** of the immediately-preceding beat — the
        writer needs the actual closing sentence to land a smooth
        承接 句, not just a summary.

        Cross-beat repetition + abstract phrasing are handled downstream
        by the critic→polish loop, not here — per-beat keyword warnings
        were a worse signal than just letting the polisher's LLM critic
        see the integrated text and judge holistically.

        ``regenerate_hint`` is set when the operator clicked
        "重寫這幕" with custom direction; the prompt forwards it so the
        re-roll diverges from the rejected text.
        """
        if await self._resolver.is_fake():
            return _synthetic_beat(beat=beat, briefs=briefs)

        full_prompt = _build_prompt(
            prompt=prompt,
            outline=outline,
            beat=beat,
            briefs=briefs,
            previously_summary=previously_summary,
            previous_tail=previous_tail,
            regenerate_hint=regenerate_hint,
            operator_primary_language=operator_primary_language,
        )
        try:
            raw = await self._resolver.generate(full_prompt)
        except Exception:
            _LOGGER.exception(
                "fusion writer LLM call failed beat sequence=%s",
                beat.sequence,
            )
            return _synthetic_beat(beat=beat, briefs=briefs)

        cleaned = _clean(raw)
        if not cleaned.strip():
            _LOGGER.warning(
                "fusion writer: empty output for beat sequence=%s",
                beat.sequence,
            )
            return _synthetic_beat(beat=beat, briefs=briefs)
        return cleaned


def _build_prompt(
    *,
    prompt: str,
    outline: FusionOutline,
    beat: FusionBeatPlan,
    briefs: Sequence[CharacterBrief],
    previously_summary: str,
    previous_tail: str,
    regenerate_hint: str | None,
    operator_primary_language: str = "zh-TW",
) -> str:
    relevant: list[CharacterBrief] = []
    if beat.focus_character_ids:
        focus_set = set(beat.focus_character_ids)
        relevant = [b for b in briefs if b.character_id in focus_set]
    if not relevant:
        # Fall back to all selected characters when the planner left
        # focus empty — better to over-share than to write a beat with
        # zero anchored persona context.
        relevant = list(briefs)

    brief_block = "\n\n".join(b.text for b in relevant)
    full_outline = "\n".join(
        f"  {b.sequence + 1}. {b.act}「{b.title}」 — {b.hook}"
        for b in outline.beats
    )
    is_first = beat.sequence == 0
    previously_block = (
        f"前面幾幕已寫的內容摘要（請保持連貫，不要重複描寫已發生的細節）：\n{previously_summary.strip()}\n"
        if previously_summary.strip()
        else "前面幾幕：（這是第一幕，不需要承接）\n"
    )

    # Transition contract — entry/exit/transition strings come from
    # the outline planner. They're optional (older outlines / fallback
    # may leave them blank), so we render whichever are filled and
    # silently skip the rest. The previous-beat tail goes here too:
    # the writer needs the real closing sentence to land 承接, not just
    # a summary line.
    transition_lines: list[str] = ["承接點（必須遵守，不能各寫各的）："]
    if not is_first and previous_tail.strip():
        transition_lines.append(
            "上一幕的收尾原文（請從這裡接續，不要重複描寫上面已寫過的場景／動作）：\n"
            f"<<<\n{previous_tail.strip()}\n>>>"
        )
    if not is_first and beat.transition_from_previous.strip():
        transition_lines.append(
            f"- 銜接方式：{beat.transition_from_previous.strip()}"
        )
    if beat.entry_state.strip():
        transition_lines.append(
            f"- 本幕開場必須在：{beat.entry_state.strip()}"
        )
    if beat.exit_state.strip():
        transition_lines.append(
            f"- 本幕結束時要落到：{beat.exit_state.strip()}（下一幕會從這裡接）"
        )
    if len(transition_lines) == 1:
        # Nothing concrete from planner + no tail (e.g. first beat with
        # blank entry_state) → drop the heading entirely so we don't
        # leave a dangling label.
        transition_block = ""
    else:
        # Trailing "\n\n" preserves the blank line that the original
        # ``"\n".join([..., transition_block, "本幕關鍵："])`` produced
        # — see the template at ``data/prompts/fusion/writer.txt``
        # which slots this directly before "本幕關鍵：".
        transition_block = "\n".join(transition_lines) + "\n\n"

    # Trailing "\n\n" mirrors the blank-line spacing the original
    # ``"\n".join([..., regenerate_block, "寫作守則："])`` produced.
    regenerate_block = (
        f"操作者要求重寫這一幕，方向是：{regenerate_hint.strip()}\n\n"
        if regenerate_hint and regenerate_hint.strip()
        else ""
    )
    body = get_default_loader().render(
        "fusion/writer",
        beat_number=beat.sequence + 1,
        beat_act=beat.act,
        beat_title=beat.title,
        target_chars=beat.target_chars,
        prompt_text=prompt.strip() or "（未指定）",
        outline_title=outline.title,
        outline_theme=outline.theme,
        outline_premise=outline.premise,
        full_outline=full_outline,
        previously_block=previously_block,
        transition_block=transition_block,
        beat_hook=beat.hook,
        beat_dramatic_question=beat.dramatic_question or "（請依 hook 自行發掘）",
        brief_block=brief_block,
        regenerate_block=regenerate_block,
    )
    language_hint = render_operator_language_hint(operator_primary_language)
    return f"{language_hint}\n\n{body}" if language_hint else body


def _clean(raw: str) -> str:
    """Strip leading meta lines / fences the LLM occasionally prepends.

    Smaller models often emit "好的，這是第二幕：" before the prose
    starts. We trim it conservatively — only the first 1~2 lines, only
    when they're short and end in a colon.
    """
    text = raw.strip()
    if text.startswith("```"):
        # Strip a leading code fence pair if the LLM ignored the rule.
        first_nl = text.find("\n")
        if first_nl > 0:
            text = text[first_nl + 1 :]
        if text.endswith("```"):
            text = text[: -3]
    text = text.strip()

    lines = text.splitlines()
    while lines and lines[0].strip().endswith(("：", ":")) and len(lines[0].strip()) <= 30:
        lines.pop(0)
    return "\n".join(lines).strip()


def _synthetic_beat(
    *,
    beat: FusionBeatPlan,
    briefs: Sequence[CharacterBrief],
) -> str:
    """Template fallback prose so polish stage still has content."""
    names = "、".join(b.short_label() for b in briefs) or "他們"
    return (
        f"（系統暫代：第 {beat.sequence + 1} 幕「{beat.title}」尚未由 LLM 寫成。）\n\n"
        f"在這一幕，{names}各自走進「{beat.title}」的場景。\n"
        f"主軸：{beat.hook}\n"
        f"懸念：{beat.dramatic_question or '（無）'}"
    )
