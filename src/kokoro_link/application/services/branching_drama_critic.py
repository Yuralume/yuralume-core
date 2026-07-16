"""LLM-backed critic for branching-drama narration.

Mirrors the role of ``FusionStoryCritic`` but adapted to drama's
shape:

- Drama narrations are short (300–500 字) — usually 1–3 paragraphs.
- The critic also sees the **prior turns of this session** so it can
  flag inter-turn repetition (the most common quality drop: the same
  emotional beat or stage direction re-used across acts).
- A single round only — no polish loop — to keep per-advance latency
  in check during gameplay.

Returns ``DramaCritique.clean()`` on fake provider / parse failure /
LLM error so the orchestrator can always continue without the polish
pass.
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
from kokoro_link.domain.entities.branching_drama import (
    DramaNode,
    DramaSessionTurn,
)
from kokoro_link.domain.value_objects.drama_critique import (
    DramaCritique,
    DramaCritiqueFinding,
    SEVERITY_CLEAN,
)
from kokoro_link.infrastructure.prompts import get_default_loader


_LOGGER = logging.getLogger(__name__)
_FENCE_RE = re.compile(r"```(?:\w+)?\n?")
_MAX_FINDINGS = 6
"""Cap on findings per pass. Drama narrations are short — past 6
findings the polisher loses focus and the prompt explodes for marginal
gain."""

_PRIOR_TURN_SNIPPET = 220
"""Per-turn excerpt budget in the prior-turns block. Keeps the critic
prompt bounded as sessions grow long."""

_PRIOR_TURN_LIMIT = 5
"""Number of most-recent prior turns to surface to the critic."""


class BranchingDramaCritic:
    """LLM-backed reviewer for drama narrations.

    Single-round design — returns a verdict that the orchestrator uses
    to decide whether to call the polisher once. No loop.
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
        node: DramaNode,
        narration_text: str,
        briefs: Sequence[CharacterBrief],
        previous_turns: Sequence[DramaSessionTurn] = (),
    ) -> DramaCritique:
        if not narration_text.strip():
            return DramaCritique.clean()
        if await self._resolver.is_fake():
            return DramaCritique.clean()

        paragraphs = _split_paragraphs(narration_text)
        full_prompt = _build_prompt(
            node=node,
            paragraphs=paragraphs,
            briefs=briefs,
            previous_turns=previous_turns,
        )
        try:
            raw = await self._resolver.generate(full_prompt)
        except Exception:
            _LOGGER.exception("drama critic LLM call failed")
            return DramaCritique.clean()

        parsed = _parse_critique(raw, paragraph_count=len(paragraphs))
        if parsed is None:
            _LOGGER.warning("drama critic: unparseable LLM output")
            return DramaCritique.clean()
        return parsed


def _split_paragraphs(text: str) -> list[str]:
    parts = [p.strip() for p in (text or "").split("\n\n")]
    return [p for p in parts if p]


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


def _build_prompt(
    *,
    node: DramaNode,
    paragraphs: Sequence[str],
    briefs: Sequence[CharacterBrief],
    previous_turns: Sequence[DramaSessionTurn],
) -> str:
    cast = "、".join(b.short_label() for b in briefs) or "（未指定）"
    enumerated = "\n\n".join(
        f"[#{i}] {p}" for i, p in enumerate(paragraphs)
    )
    max_index = max(0, len(paragraphs) - 1)
    prior_block = _summarise_prior_turns(previous_turns)
    tone_line = (
        f"本段取向：{node.tone}" if node.tone else "本段取向：（未指定）"
    )

    return get_default_loader().render(
        "branching/critic",
        cast=cast,
        node_title=node.title,
        node_summary=node.summary,
        tone_line=tone_line,
        prior_block=prior_block,
        max_index=max_index,
        enumerated=enumerated,
        max_findings=_MAX_FINDINGS,
    )


def _parse_critique(
    raw: str, *, paragraph_count: int,
) -> DramaCritique | None:
    if not raw:
        return None
    cleaned = _FENCE_RE.sub("", raw).strip().rstrip("`")
    try:
        obj: Any = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    try:
        severity = int(obj.get("severity", 0))
    except (TypeError, ValueError):
        return None
    if severity < SEVERITY_CLEAN:
        severity = SEVERITY_CLEAN
    summary = str(obj.get("summary", "") or "")
    findings_raw = obj.get("findings") or []
    if not isinstance(findings_raw, list):
        findings_raw = []
    findings: list[DramaCritiqueFinding] = []
    for entry in findings_raw[:_MAX_FINDINGS]:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("kind") or "").strip()
        issue = str(entry.get("issue") or "").strip()
        if not kind or not issue:
            continue
        idx_raw = entry.get("paragraph_index")
        idx: int | None = None
        if isinstance(idx_raw, int):
            idx = idx_raw if 0 <= idx_raw < paragraph_count else None
        try:
            findings.append(
                DramaCritiqueFinding.create(
                    kind=kind,
                    quote=str(entry.get("quote") or ""),
                    issue=issue,
                    suggestion=str(entry.get("suggestion") or ""),
                    paragraph_index=idx,
                )
            )
        except ValueError:
            continue
    return DramaCritique.create(
        severity=severity, summary=summary, findings=findings,
    )
