"""Port for the proactive pre-send intention judgement.

This judge sits after the cheap gate and before message composition. It
does not write the outbound message. Its job is to decide whether the
character has a strong enough inner motive to spend one of today's scarce
proactive slots right now.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kokoro_link.contracts.proactive import ProactiveContext


@dataclass(frozen=True, slots=True)
class ProactiveIntentionDecision:
    should_consume_slot: bool
    """Whether this evaluation should continue to message composition."""

    reason: str
    """Short operator-facing reason for the audit log."""

    inner_motive: str = ""
    conversation_purpose: str = ""
    expected_reply: str = ""
    risk: str = ""
    best_timing: str = ""

    judge_unavailable: bool = False
    """True when this is a *fail-soft fallback*, not a judgement: the
    judge could not reach its model, or got back something it could not
    read, and defaulted to not spending the slot.

    Both cases surface as ``should_consume_slot=False`` and are
    indistinguishable from an authentic "not now" by value alone — hence
    a structured flag rather than reading the ``reason`` text, which is
    operator-facing prose in the operator's own language.

    Callers that hold state riding on this tick (the dispatcher's spent
    revisit alarm) use it to tell "the character looked and decided" from
    "nobody ever looked"."""

    revisit_at_iso: str = ""
    """A concrete future moment the judge itself named as "the time this
    becomes appropriate" — e.g. the pair already agreed on 19:30, or the
    user said they're free after 21:00.

    Local civil time in the character's timezone (``YYYY-MM-DDTHH:MM``),
    empty when the judge only has a vague "later". Distinct from
    ``best_timing``, which is a free-text mood word (``evening`` /
    ``later``) and stays unstructured on purpose: this field is the one
    the dispatcher can act on, so it is deliberately narrow — the
    difference between the two is a semantic judgement the LLM makes,
    never a keyword rule downstream."""


class ProactiveIntentionJudgePort(Protocol):
    async def judge(
        self, context: ProactiveContext,
    ) -> ProactiveIntentionDecision: ...
