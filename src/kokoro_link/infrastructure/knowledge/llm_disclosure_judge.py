"""LLM adapter for :class:`PlayerKnowledgeDisclosureJudgePort` (KB8).

One short model call per *successfully delivered* proactive push: the
message text plus the ``private`` memories that went into composing it,
a one-field JSON answer out.

Two parsing decisions are policy, not plumbing
----------------------------------------------
**Truncation repair is off.** The shared extraction layer
(:mod:`kokoro_link.llm_output`) can close a reply the upstream cut short,
and for a payload — a list of memories, a schedule — that rescue is free
value. This is not a payload; it is a conclusion, and repairing a
cut-off one means inventing the half the model never sent and then
writing it into a ledger that has no reverse transition. An unparseable
reply is a judge failure, and a judge failure discloses nothing. (Same
call as the outcome-claim judge, DH1.)

**Ids are allow-listed, never validated.** Only ids present in the
candidate set survive; an unrecognised one is dropped without a lookup.
Checking "does this id exist?" instead would let a hallucinated-but-real
id mark an untold memory as told — the failure the whole candidate
mechanism exists to make impossible.

The fake backend short-circuits to "nothing disclosed" rather than to a
failure: it emits deterministic junk no schema survives, and a dev
deployment does not need a permanent judge-outage signal for a channel
whose messages are placeholder text anyway.
"""

from __future__ import annotations

import logging

from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.player_knowledge_disclosure import (
    DisclosureCandidate,
    DisclosureVerdict,
    PlayerKnowledgeDisclosureJudgePort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.infrastructure.prompt.player_knowledge_disclosure import (
    DISCLOSED_FIELD,
    render_disclosure_judge_prompt,
)
from kokoro_link.llm_output import (
    extract_object_outcome,
    first_region_is_array,
    log_parse_outcome,
)

_LOGGER = logging.getLogger(__name__)

_PARSE_SITE = "knowledge.disclosure_judge"


class LLMDisclosureJudge(PlayerKnowledgeDisclosureJudgePort):
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
        candidates: tuple[DisclosureCandidate, ...],
        character: Character | None = None,
    ) -> DisclosureVerdict:
        text = (message_text or "").strip()
        if not text or not candidates:
            # Nothing was said, or nothing could have been disclosed.
            # Neither is a judgement worth paying a model for, and both
            # have the same answer.
            return DisclosureVerdict.none()
        try:
            if await self._resolver.is_fake(character=character):
                return DisclosureVerdict.none()
        except Exception:
            _LOGGER.exception(
                "disclosure judge: provider probe failed character=%s",
                getattr(character, "id", "?"),
            )
            return DisclosureVerdict.failed()
        prompt = render_disclosure_judge_prompt(
            message_text=text, candidates=candidates,
        )
        try:
            raw = await self._resolver.generate(prompt, character=character)
        except Exception:
            _LOGGER.exception(
                "disclosure judge: LLM call failed character=%s",
                getattr(character, "id", "?"),
            )
            return DisclosureVerdict.failed()
        return _parse_verdict(
            raw, allowed={c.memory_id for c in candidates},
        )


class NullDisclosureJudge(PlayerKnowledgeDisclosureJudgePort):
    """Always "nothing disclosed" — the explicit "this deployment does
    not run the proactive half of the ledger" object, so a caller never
    branches on ``judge is None``. Its answer is a real verdict, not a
    failure: a deployment that chose not to judge is not an outage."""

    async def judge(
        self,
        *,
        message_text: str,
        candidates: tuple[DisclosureCandidate, ...],
        character: Character | None = None,
    ) -> DisclosureVerdict:
        return DisclosureVerdict.none()


def _parse_verdict(raw: str, *, allowed: set[str]) -> DisclosureVerdict:
    """Read the one-field answer, bounded by the candidate ids.

    An array-shaped reply is refused before the object extractor runs:
    the extractor would reach into ``[{...}]`` and return its first
    element, which here means acting on one of several answers the model
    could not choose between.
    """
    text = raw or ""
    if first_region_is_array(text):
        _LOGGER.warning(
            "disclosure judge: reply was array-shaped where one object "
            "was asked for — treating as a judge failure",
        )
        return DisclosureVerdict.failed()
    outcome = extract_object_outcome(text, repair_truncated=False)
    log_parse_outcome(_LOGGER, outcome, site=_PARSE_SITE)
    payload = outcome.value
    if not isinstance(payload, dict):
        return DisclosureVerdict.failed()
    value = payload.get(DISCLOSED_FIELD)
    if not isinstance(value, list):
        _LOGGER.warning(
            "disclosure judge: %r was %s, not a list — treating as a "
            "judge failure rather than as an empty answer",
            DISCLOSED_FIELD, type(value).__name__,
        )
        return DisclosureVerdict.failed()
    seen: set[str] = set()
    kept: list[str] = []
    for entry in value:
        if not isinstance(entry, str):
            continue
        item_id = entry.strip()
        if item_id in seen or item_id not in allowed:
            continue
        seen.add(item_id)
        kept.append(item_id)
    return DisclosureVerdict.of(tuple(kept))


__all__ = ["LLMDisclosureJudge", "NullDisclosureJudge"]
