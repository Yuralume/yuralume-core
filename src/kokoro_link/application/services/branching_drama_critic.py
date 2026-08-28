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

import logging
import re
from collections.abc import Sequence

from kokoro_link.application.services.fusion_character_brief import (
    CharacterBrief,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.branching_drama import (
    DEFAULT_OPERATOR_POSITION,
    DramaNode,
    DramaSessionTurn,
)
from kokoro_link.infrastructure.prompt.drama_operator_position_lines import (
    STAGE_CRITIC,
    render_drama_operator_position_block,
)
from kokoro_link.domain.value_objects.drama_critique import (
    DramaCritique,
    DramaCritiqueFinding,
    SEVERITY_CLEAN,
)
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.llm_output import (
    extract_object_outcome,
    first_region_is_array,
    log_parse_outcome,
)


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
        operator_position: str = DEFAULT_OPERATOR_POSITION,
        operator_note: str | None = None,
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
            operator_position=operator_position,
            operator_note=operator_note,
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
    operator_position: str = DEFAULT_OPERATOR_POSITION,
    operator_note: str | None = None,
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

    body = get_default_loader().render(
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
    # BD2, on the code side — the shipped prompt pack is frozen. Without
    # it the critic reviews every narration against the 主演 framing and
    # would flag an 旁觀者 drama's third-person prose as a defect, undoing
    # exactly what the player asked for.
    #
    # Prefixed, not appended: the template ends with its 輸出規則 block and
    # anything past that competes with the JSON contract ``_parse_critique``
    # depends on.
    position_block = render_drama_operator_position_block(
        operator_position, operator_note, stage=STAGE_CRITIC,
    )
    return f"{position_block}\n\n{body}" if position_block else body


def _parse_critique(
    raw: str, *, paragraph_count: int,
) -> DramaCritique | None:
    if not raw:
        return None
    cleaned = _FENCE_RE.sub("", raw).strip().rstrip("`")
    # Old behaviour, preserved: the old parser was a whole-string
    # ``json.loads`` only. When that succeeded with a non-dict value
    # (the model wrapped its verdict in ``[...]``), the isinstance
    # check right below discarded it — a balanced-scan extractor must
    # not dig past that value for a plausible-looking nested object and
    # smuggle a fragment through the very check meant to reject it.
    #
    # FX1/DH-2: that guard is asked structurally now. Spelled as "the
    # whole reply decodes and isn't a dict" it needed the *entire*
    # string to parse, so the one thing models actually do — close the
    # array, then add a closing remark — switched it off completely.
    if first_region_is_array(cleaned):
        return None
    # DH2-services: truncation repair is on — the critic's prompt
    # unconditionally asks for JSON, so a reply chopped by max_tokens
    # now still yields a verdict instead of silently falling back to
    # ``DramaCritique.clean()``.
    outcome = extract_object_outcome(raw)
    log_parse_outcome(_LOGGER, outcome, site="branching_drama.critic")
    obj = outcome.value
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
