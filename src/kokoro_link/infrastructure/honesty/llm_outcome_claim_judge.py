"""LLM-backed adapter for :class:`OutcomeClaimJudgePort` (HV1).

One short model call per outbound background message: prompt in
(:mod:`kokoro_link.infrastructure.prompt.outcome_claim_honesty`), a
two-field JSON verdict out.

Two things about the parsing are policy, not plumbing
-----------------------------------------------------
**Truncation repair is off.** The shared extraction layer
(:mod:`kokoro_link.llm_output`) can close a reply the upstream cut short,
and for a *payload* — a list of memories, a schedule — that rescue is
free value. A verdict is not a payload; it is a conclusion, and the field
that carries it is two words long. Repairing a cut-off verdict means
inventing the half the model never sent and then acting on it as a gate
decision. The whole point of the gate is that it does not guess, so an
unparseable reply is a **judge failure** and the caller parks.

**An unrecognised verdict word is a failure too**, for the same reason:
"maybe" / "不確定" / an empty string are not approval. Only the two words
the prompt asked for count, and everything else routes to
:meth:`OutcomeClaimVerdict.failed` where the caller's fail-closed rule
applies.

The fake backend is the one deliberate exception. It emits deterministic
junk that no schema survives, so treating it as a judge outage would park
every promise on a dev deployment — one whose composers already return
empty text and therefore never produce a claim to check. It answers
``consistent`` and gets out of the way.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import Any

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.outcome_claim import (
    OutcomeClaimEvidence,
    OutcomeClaimJudgePort,
    OutcomeClaimVerdict,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.prompt.outcome_claim_honesty import (
    CLAIMS_FIELD,
    VERDICT_CONSISTENT,
    VERDICT_FIELD,
    VERDICT_INCONSISTENT,
    message_is_truncated_for_judge,
    render_outcome_claim_judge_prompt,
)
from kokoro_link.llm_output import (
    extract_object_outcome,
    first_region_is_array,
    log_parse_outcome,
)

_LOGGER = logging.getLogger(__name__)

_PARSE_SITE = "honesty.outcome_claim_judge"

_MAX_CLAIMS = 6
_MAX_CLAIM_CHARS = 160


class LLMOutcomeClaimJudge(OutcomeClaimJudgePort):
    def __init__(
        self,
        model: ChatModelPort | None = None,
        *,
        provider: ActiveLLMProviderPort | None = None,
        feature_key: str | None = None,
    ) -> None:
        self._resolver = ModelResolver(
            provider=provider, model=model, feature_key=feature_key,
        )

    async def judge(
        self,
        *,
        message_text: str,
        evidence: OutcomeClaimEvidence,
        character: Character | None = None,
        operator_primary_language: str = "",
    ) -> OutcomeClaimVerdict:
        """``character`` routes the call; it never reaches the prompt.

        ``operator_primary_language`` is accepted for port symmetry and
        deliberately unused: the verdict is a machine value, not prose a
        player reads, and rendering the instruction in the operator's
        language would only add a translation surface to a gate."""
        text = (message_text or "").strip()
        if not text:
            # Nothing was written, so nothing was claimed. Not a judgement
            # the model needs to be paid for.
            return OutcomeClaimVerdict.ok()
        try:
            if await self._resolver.is_fake(character=character):
                return OutcomeClaimVerdict.ok()
        except Exception:
            _LOGGER.exception(
                "outcome-claim judge: provider probe failed character=%s",
                getattr(character, "id", "?"),
            )
            return OutcomeClaimVerdict.failed()
        prompt = render_outcome_claim_judge_prompt(
            message_text=text, evidence=evidence,
        )
        try:
            raw = await self._resolver.generate(prompt, character=character)
        except Exception:
            _LOGGER.exception(
                "outcome-claim judge: LLM call failed character=%s",
                getattr(character, "id", "?"),
            )
            return OutcomeClaimVerdict.failed()
        # S5: computed the same way the prompt truncated ``text`` (same
        # predicate, same input), so a verdict reached over only a prefix
        # is marked as such — regardless of which of the two answers the
        # model gave. ``OutcomeClaimGuard`` is where that fact turns into
        # a trace; this adapter's only job is to not lose it.
        truncated = message_is_truncated_for_judge(text)
        verdict = _parse_verdict(raw)
        if truncated and not verdict.unavailable:
            verdict = replace(verdict, truncated=True)
        return verdict


class NullOutcomeClaimJudge(OutcomeClaimJudgePort):
    """Always consistent — the explicit "this deployment does not gate"
    object, so a caller never has to branch on ``judge is None``."""

    async def judge(
        self,
        *,
        message_text: str,
        evidence: OutcomeClaimEvidence,
        character: Character | None = None,
        operator_primary_language: str = "",
    ) -> OutcomeClaimVerdict:
        return OutcomeClaimVerdict.ok()


def _parse_verdict(raw: str) -> OutcomeClaimVerdict:
    """Read the two-field verdict object, or report a judge failure.

    Array-shaped replies are refused before the object extractor runs:
    the extractor would happily reach into ``[{...}]`` and hand back the
    first element, which for a *gate* means acting on one of several
    answers the model could not choose between."""
    text = raw or ""
    if first_region_is_array(text):
        _LOGGER.warning(
            "outcome-claim judge: reply was array-shaped where one verdict "
            "object was asked for — treating as a judge failure",
        )
        return OutcomeClaimVerdict.failed()
    outcome = extract_object_outcome(text, repair_truncated=False)
    log_parse_outcome(_LOGGER, outcome, site=_PARSE_SITE)
    payload = outcome.value
    if not isinstance(payload, dict):
        return OutcomeClaimVerdict.failed()
    verdict = payload.get(VERDICT_FIELD)
    if not isinstance(verdict, str):
        _LOGGER.warning(
            "outcome-claim judge: missing %r field — treating as a judge "
            "failure", VERDICT_FIELD,
        )
        return OutcomeClaimVerdict.failed()
    normalized = verdict.strip().lower()
    if normalized == VERDICT_CONSISTENT:
        return OutcomeClaimVerdict.ok()
    if normalized == VERDICT_INCONSISTENT:
        return OutcomeClaimVerdict.blocked(_claims(payload.get(CLAIMS_FIELD)))
    _LOGGER.warning(
        "outcome-claim judge: unrecognised verdict %r — treating as a judge "
        "failure rather than approval", normalized[:40],
    )
    return OutcomeClaimVerdict.failed()


def _claims(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    cleaned: list[str] = []
    for item in value[:_MAX_CLAIMS]:
        if not isinstance(item, str):
            continue
        text = item.strip()[:_MAX_CLAIM_CHARS]
        if text:
            cleaned.append(text)
    return tuple(cleaned)


__all__ = ["LLMOutcomeClaimJudge", "NullOutcomeClaimJudge"]
