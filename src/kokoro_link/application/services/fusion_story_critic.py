"""LLM-backed critic stage for the fusion-story pipeline.

The critic reads a polished draft and decides whether another polish
round is justified — and if so, what *specifically* the next round
must fix. It does **not** rewrite anything; the polisher does. The
two stages run in a loop until the critic returns
``SEVERITY_CLEAN`` (or ``should_continue == False``) or the
orchestrator's round cap kicks in.

Why a dedicated LLM call instead of a hand-curated repetition list:

- A keyword list catches "眼眸 / 嘴角微揚" but misses every novel
  repetition the model wanders into ("她看向窗外" appearing six times
  with different objects on the other side). Semantic detection
  handles both.
- "Abstract / vague" is fundamentally a judgement, not a pattern
  match. Asking another LLM to read the draft and point at the
  thinnest paragraphs lets us improve them; word lists can't tell
  abstract-but-elegant from abstract-and-empty.
- The critic can also flag soft transitions, voice drift, telling-
  rather-than-showing — anything the writer-then-polisher pipeline
  let through. One prompt, broad coverage.

Falls back to a clean verdict on any error so the loop terminates
gracefully rather than crashing the pipeline mid-polish.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Sequence
from typing import Any

from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.value_objects.fusion_critique import (
    FusionCritiqueFinding,
    FusionStoryCritique,
    SEVERITY_CLEAN,
    SEVERITY_SEVERE,
)
from kokoro_link.domain.value_objects.fusion_outline import FusionOutline
from kokoro_link.infrastructure.prompts import get_default_loader


_LOGGER = logging.getLogger(__name__)
_FENCE_RE = re.compile(r"```(?:\w+)?\n?")
_MAX_FINDINGS = 10
"""Cap on findings per round — the polisher loses focus past this and
the prompt explodes. Critic is asked to prioritise the worst issues
when it has more than this many to report."""


class FusionStoryCritic:
    """LLM-backed reviewer. Returns ``FusionStoryCritique.clean()`` on
    fake provider / error so the polish loop can always terminate.
    """

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

    async def review(
        self,
        *,
        prompt: str,
        outline: FusionOutline,
        draft_text: str,
        briefs: Sequence[CharacterBrief],
        round_index: int = 0,
        previous_critique: FusionStoryCritique | None = None,
    ) -> FusionStoryCritique:
        """Return the critic's verdict on ``draft_text``.

        ``round_index`` (0-indexed) lets the prompt tell the LLM what
        round this is so later rounds become stricter / more
        diminishing-returns-aware. ``previous_critique`` carries the
        prior round's findings so the critic can notice "the polisher
        didn't actually fix what I asked for last time" and escalate
        rather than re-flagging the same line.
        """
        if not draft_text.strip():
            return FusionStoryCritique.clean()
        if await self._resolver.is_fake():
            return FusionStoryCritique.clean()

        paragraphs = _split_paragraphs(draft_text)
        full_prompt = _build_prompt(
            prompt=prompt,
            outline=outline,
            paragraphs=paragraphs,
            briefs=briefs,
            round_index=round_index,
            previous_critique=previous_critique,
        )
        try:
            raw = await self._resolver.generate(full_prompt)
        except Exception:
            _LOGGER.exception("fusion critic LLM call failed")
            return FusionStoryCritique.clean()

        parsed = _parse_critique(raw, paragraph_count=len(paragraphs))
        if parsed is None:
            _LOGGER.warning("fusion critic: unparseable LLM output")
            return FusionStoryCritique.clean()
        return parsed


# --- prompt + parsing -------------------------------------------------


def _build_prompt(
    *,
    prompt: str,
    outline: FusionOutline,
    paragraphs: Sequence[str],
    briefs: Sequence[CharacterBrief],
    round_index: int,
    previous_critique: FusionStoryCritique | None,
) -> str:
    cast = "、".join(b.short_label() for b in briefs) or "（未指定）"
    outline_block = "\n".join(
        f"  {b.sequence + 1}. {b.act}「{b.title}」 — {b.hook}"
        for b in outline.beats
    )
    transition_block = _render_transition_spec(outline)

    # Render the draft with explicit paragraph indices so the critic
    # can cite which paragraph each finding lives in. The polisher uses
    # these indices to scope spot-rewrites; without them every finding
    # would force a whole-piece rewrite.
    enumerated_draft = "\n\n".join(
        f"[#{i}] {p}" for i, p in enumerate(paragraphs)
    )
    max_index = max(0, len(paragraphs) - 1)

    previous_block = ""
    if previous_critique is not None and previous_critique.findings:
        previous_block = (
            "上一輪你（critic）已經點過下列問題；如果這一稿仍未解決，"
            "請在 issue 裡明確指出『polisher 未處理』，並把 severity 拉高一級：\n"
            + _render_findings(previous_critique.findings)
            + "\n\n"
        )

    return get_default_loader().render(
        "fusion/critic",
        round_number=round_index + 1,
        theme=outline.theme,
        title=outline.title,
        cast=cast,
        prompt_text=prompt.strip() or "（未指定）",
        outline_block=outline_block,
        transition_block=transition_block,
        max_index=max_index,
        enumerated_draft=enumerated_draft,
        previous_block=previous_block,
        max_findings=_MAX_FINDINGS,
    )


def _split_paragraphs(draft_text: str) -> list[str]:
    """Split the draft on blank lines into paragraph spans.

    Empty results are filtered so the index space matches what the
    polisher will see when it re-splits the same string. Keeping the
    splitter centralised means critic and polisher can't drift on what
    counts as a paragraph boundary.
    """
    parts = [p.strip() for p in (draft_text or "").split("\n\n")]
    return [p for p in parts if p]


def _render_transition_spec(outline: FusionOutline) -> str:
    """Echo the outline's transition contract so the critic can compare
    spec vs. delivered text. Empty when the outline didn't fill any
    transition fields (older saved stories)."""
    lines: list[str] = []
    for b in outline.beats:
        bits: list[str] = [f"幕 {b.sequence + 1}「{b.title}」"]
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
    return "幕間轉場規劃（成品應該符合）：\n" + "\n".join(lines) + "\n"


def _render_findings(
    findings: Sequence[FusionCritiqueFinding],
) -> str:
    parts: list[str] = []
    for i, f in enumerate(findings, start=1):
        quote_hint = f" 引文：「{f.quote}」" if f.quote else ""
        parts.append(
            f"  {i}. [{f.kind}] {f.issue}{quote_hint}"
            + (f" 建議：{f.suggestion}" if f.suggestion else "")
        )
    return "\n".join(parts)


def _parse_critique(
    raw: str, *, paragraph_count: int,
) -> FusionStoryCritique | None:
    text = _FENCE_RE.sub("", raw or "").replace("```", "").strip()
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    severity = _coerce_severity(data.get("severity"))
    summary = _coerce_str(data.get("summary"))
    should_continue = data.get("should_continue")
    if not isinstance(should_continue, bool):
        # Default: continue if severity > 0, stop otherwise.
        should_continue = severity > SEVERITY_CLEAN
    findings = _coerce_findings(
        data.get("findings"), paragraph_count=paragraph_count,
    )
    try:
        return FusionStoryCritique.create(
            severity=severity,
            summary=summary,
            findings=findings,
            should_continue=should_continue,
        )
    except ValueError:
        return None


def _coerce_severity(raw: Any) -> int:
    if isinstance(raw, bool):
        return SEVERITY_CLEAN
    if isinstance(raw, (int, float)):
        value = int(raw)
        if value < SEVERITY_CLEAN:
            return SEVERITY_CLEAN
        if value > SEVERITY_SEVERE:
            return SEVERITY_SEVERE
        return value
    if isinstance(raw, str):
        try:
            return _coerce_severity(int(raw.strip()))
        except ValueError:
            return SEVERITY_CLEAN
    return SEVERITY_CLEAN


def _coerce_str(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    return ""


def _coerce_findings(
    raw: Any, *, paragraph_count: int,
) -> list[FusionCritiqueFinding]:
    if not isinstance(raw, list):
        return []
    out: list[FusionCritiqueFinding] = []
    for entry in raw[:_MAX_FINDINGS]:
        if not isinstance(entry, dict):
            continue
        kind = _coerce_str(entry.get("kind"))
        issue = _coerce_str(entry.get("issue"))
        if not kind or not issue:
            # A finding without a kind or issue is noise; drop it
            # rather than synthesising a placeholder.
            continue
        paragraph_index = _coerce_paragraph_index(
            entry.get("paragraph_index"), paragraph_count=paragraph_count,
        )
        try:
            out.append(
                FusionCritiqueFinding.create(
                    kind=kind,
                    quote=_coerce_str(entry.get("quote")),
                    issue=issue,
                    suggestion=_coerce_str(entry.get("suggestion")),
                    paragraph_index=paragraph_index,
                ),
            )
        except ValueError:
            continue
    return out


def _coerce_paragraph_index(
    raw: Any, *, paragraph_count: int,
) -> int | None:
    """Return a valid in-range paragraph index, or ``None`` for whole-
    story observations / out-of-range / missing values.

    Out-of-range values silently become None — better to fall back to a
    global polish than to anchor on a phantom paragraph and corrupt the
    rejoin.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        idx = int(raw)
    elif isinstance(raw, str):
        try:
            idx = int(raw.strip())
        except ValueError:
            return None
    else:
        return None
    if idx < 0 or idx >= paragraph_count:
        return None
    return idx
