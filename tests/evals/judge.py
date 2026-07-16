"""LLM-as-judge harness.

Given a fixture rubric and a candidate response, asks an LLM to score
the candidate. Output is parsed into ``JudgeVerdict``; parse failures
fall back to a structured error so the runner can mark the fixture
inconclusive rather than silently pass.

Pre-check filters (``must_include_concepts`` / ``must_not_include_concepts``)
run before the LLM call — cheap deterministic short-circuit that catches
gross failures without spending tokens. Keyword matching is substring,
case-insensitive, CJK-safe, and whitespace-insensitive.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

from kokoro_link.contracts.llm import ChatModelPort

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class JudgeCriteria:
    rubric: str
    """Free-text scoring guide handed to the judge model verbatim."""
    must_include_concepts: tuple[str, ...] = ()
    """Cheap substring pre-check. Empty = skip pre-check."""
    must_not_include_concepts: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    passed: bool
    score: float
    """0.0 to 1.0; judge-assigned. Pre-check fail → 0.0."""
    reasons: tuple[str, ...] = field(default_factory=tuple)
    raw_response: str = ""
    pre_check_failure: str | None = None
    """Set when the deterministic keyword pre-check vetoed; no LLM call
    happened. ``None`` means the LLM judge produced the verdict."""


_JUDGE_PROMPT_TEMPLATE = """\
You are evaluating whether an AI character's response meets a behavioural rubric.

# Rubric
{rubric}

# Candidate response
{candidate}

# Output format
Respond with a single JSON object, no prose around it:

{{
  "passed": true|false,
  "score": 0.0,
  "reasons": ["short bullet 1", "short bullet 2"]
}}

- ``passed`` is the binary verdict.
- ``score`` is 0.0 to 1.0, your overall confidence the response meets the rubric.
- ``reasons`` is 1-3 short strings (≤ 80 chars each) citing concrete evidence.
"""


def _normalise(text: str) -> str:
    return text.lower()


def _compact(text: str) -> str:
    return "".join(_normalise(text).split())


def _pre_check(
    candidate: str, criteria: JudgeCriteria,
) -> str | None:
    """Return a failure reason if the candidate trips the keyword guards.

    The same string is shown to the user — so callers can decide whether
    to skip the LLM call entirely. Returns ``None`` on success.
    """
    text = _normalise(candidate)
    compact_text = _compact(candidate)
    for needle in criteria.must_include_concepts:
        if not needle.strip():
            continue
        normalized = _normalise(needle)
        compact_needle = _compact(needle)
        if normalized not in text and compact_needle not in compact_text:
            return f"missing required concept: {needle!r}"
    for forbidden in criteria.must_not_include_concepts:
        if not forbidden.strip():
            continue
        normalized = _normalise(forbidden)
        compact_forbidden = _compact(forbidden)
        if normalized in text or compact_forbidden in compact_text:
            return f"forbidden phrase appeared: {forbidden!r}"
    return None


_JSON_BLOCK_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json_block(text: str) -> str | None:
    """Pull the first ``{...}`` substring out of a model response.

    LM Studio / Anthropic / OpenAI all sometimes wrap JSON in prose
    despite explicit instructions; this lets us recover the structured
    bit without forcing strict JSON mode.
    """
    match = _JSON_BLOCK_RE.search(text)
    return match.group(0) if match else None


async def evaluate(
    *,
    judge_model: ChatModelPort,
    candidate: str,
    criteria: JudgeCriteria,
    judge_model_id: str | None = None,
) -> JudgeVerdict:
    pre_failure = _pre_check(candidate, criteria)
    if pre_failure is not None:
        return JudgeVerdict(
            passed=False,
            score=0.0,
            reasons=(pre_failure,),
            pre_check_failure=pre_failure,
        )

    prompt = _JUDGE_PROMPT_TEMPLATE.format(
        rubric=criteria.rubric.strip(),
        candidate=candidate.strip(),
    )
    try:
        raw = await judge_model.generate(prompt, model=judge_model_id)
    except Exception as exc:  # noqa: BLE001 — surface as failed verdict
        _LOGGER.exception("judge model call failed")
        return JudgeVerdict(
            passed=False,
            score=0.0,
            reasons=(f"judge model error: {exc!r}",),
            raw_response="",
        )

    block = _extract_json_block(raw)
    if block is None:
        return JudgeVerdict(
            passed=False, score=0.0,
            reasons=("judge response had no JSON block",),
            raw_response=raw,
        )
    try:
        parsed = json.loads(block)
    except json.JSONDecodeError as exc:
        return JudgeVerdict(
            passed=False, score=0.0,
            reasons=(f"judge JSON parse failed: {exc}",),
            raw_response=raw,
        )

    passed = bool(parsed.get("passed"))
    raw_score = parsed.get("score", 0.0)
    try:
        score = max(0.0, min(1.0, float(raw_score)))
    except (TypeError, ValueError):
        score = 0.0
    reasons_raw = parsed.get("reasons") or []
    if isinstance(reasons_raw, str):
        reasons_raw = [reasons_raw]
    reasons = tuple(str(r) for r in reasons_raw if isinstance(r, (str, int, float)))
    return JudgeVerdict(
        passed=passed,
        score=score,
        reasons=reasons,
        raw_response=raw,
    )
