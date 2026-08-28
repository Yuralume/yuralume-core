"""Outcome-claim honesty port (HV1).

The question this port answers is narrow and structural: **does a message
about to reach a player claim a concrete, already-completed external
action that the evidence does not support?**

It exists because the two-pass promise loop
(:mod:`kokoro_link.application.services.composer_tool_loop`) has two
exits where a model can hand back prose that says "畫好了，附上照片" while
``tool_invocations`` for that round is empty — the shape observed in
production on 2026-08-25. A mechanical guard cannot see it: the sentence
is well-formed prose, and any keyword table ("照片" / "查到了") is both
trivially evadable and forbidden by the project's top directive. So the
judgement is semantic, and it is a separate model call.

What the judge is given, and what it is deliberately NOT given
--------------------------------------------------------------
Given: the final text, the names of the tools this round *offered*, the
outcomes those tools actually returned, and how many attachments will
ship with the message.

Not given: persona, memory, relationship, schedule — anything that would
let it reason about *what the character would plausibly have done*. That
omission is the red line, not an oversight: a judge that knows the
character is a photographer starts explaining away a claimed photo. The
only admissible evidence is what the tools did.

Three things are explicitly **not** dishonest, and the prompt says so —
they are the boundary a keyword matcher cannot draw:

* a promise about the future ("晚點傳給你"),
* an action inside the fiction ("我走過去把窗簾拉上"),
* repeating material the player themselves supplied.

Fail direction
--------------
:data:`OUTCOME_CLAIM_UNAVAILABLE` is a **third** answer, never folded into
"consistent". A judge that cannot answer — upstream down, verdict
unparseable — must not be read as approval; the background caller parks
instead (§3.4 fail-closed). Callers that want the old behaviour simply do
not wire a judge at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kokoro_link.contracts.prompt import ToolOutcomeMessage
from kokoro_link.domain.entities.character import Character

OUTCOME_CLAIM_CONSISTENT = "consistent"
"""The text claims nothing the evidence cannot back."""

OUTCOME_CLAIM_INCONSISTENT = "inconsistent"
"""The text claims a completed external action with no matching evidence."""

OUTCOME_CLAIM_UNAVAILABLE = "unavailable"
"""No verdict could be produced (call failed, or the reply would not
parse). Distinct from ``consistent`` on purpose — see the module
docstring."""


@dataclass(frozen=True, slots=True)
class OutcomeClaimEvidence:
    """Everything that actually happened on this composition round.

    ``offered_tools`` matters as much as ``outcomes``: "you were given a
    camera and did not pick it up" is exactly the zero-call case, and a
    judge that only saw an empty outcome list could not tell it apart
    from a deployment that has no camera at all.

    ``delivered_attachments`` counts what will ship **with this message**,
    after the delivery filter — not what a tool produced. A render whose
    URL was dropped for want of a public base URL is a file the player
    will never see, so a message claiming "照片傳過去了" is a lie the
    player can check, and the judge must be told the shipping number.
    """

    offered_tools: tuple[str, ...] = ()
    outcomes: tuple[ToolOutcomeMessage, ...] = ()
    delivered_attachments: int = 0

    @property
    def any_tool_ran(self) -> bool:
        return bool(self.outcomes)


@dataclass(frozen=True, slots=True)
class OutcomeClaimVerdict:
    """The judge's answer, plus what it objected to.

    ``unsupported_claims`` is short prose lifted from the message, kept
    for the log / audit trail and for the correction instruction the loop
    feeds back into the retry. It is **never** shown to the player: the
    player must not read the character being told off.

    ``truncated`` (S5) marks a verdict reached over only a *prefix* of the
    message — set whenever the judge was actually asked (never on the
    empty-text / fake-provider short-circuits, which never looked at the
    text at all). It does not change ``status``: a ``consistent`` verdict
    over a truncated message is still recorded as ``consistent`` — the
    fix is that this fact is no longer invisible. See
    :mod:`kokoro_link.infrastructure.honesty.llm_outcome_claim_judge` for
    where it is set and :class:`~kokoro_link.application.services.
    outcome_claim_guard.OutcomeClaimGuard` for where it leaves its trace
    (a counter and an audit-event field, never a silently-upgraded
    status)."""

    status: str
    unsupported_claims: tuple[str, ...] = ()
    truncated: bool = False

    @property
    def consistent(self) -> bool:
        return self.status == OUTCOME_CLAIM_CONSISTENT

    @property
    def inconsistent(self) -> bool:
        return self.status == OUTCOME_CLAIM_INCONSISTENT

    @property
    def unavailable(self) -> bool:
        return self.status == OUTCOME_CLAIM_UNAVAILABLE

    @classmethod
    def ok(cls, *, truncated: bool = False) -> "OutcomeClaimVerdict":
        return cls(status=OUTCOME_CLAIM_CONSISTENT, truncated=truncated)

    @classmethod
    def blocked(
        cls, claims: tuple[str, ...] = (), *, truncated: bool = False,
    ) -> "OutcomeClaimVerdict":
        return cls(
            status=OUTCOME_CLAIM_INCONSISTENT,
            unsupported_claims=claims,
            truncated=truncated,
        )

    @classmethod
    def failed(cls) -> "OutcomeClaimVerdict":
        return cls(status=OUTCOME_CLAIM_UNAVAILABLE)


class OutcomeClaimJudgePort(Protocol):
    async def judge(
        self,
        *,
        message_text: str,
        evidence: OutcomeClaimEvidence,
        character: Character | None = None,
        operator_primary_language: str = "",
    ) -> OutcomeClaimVerdict:
        """Judge one outbound message against one round's tool facts.

        ``character`` is a **routing** argument only — it selects the
        per-character feature model, exactly as every other auxiliary
        judge does. Implementations must not put any of its persona
        fields into the prompt (see the module docstring red line);
        ``tests/unit/test_outcome_claim_judge.py`` pins that.

        Never raises: a judge that dies must return
        :meth:`OutcomeClaimVerdict.failed` so the caller can apply its own
        fail direction rather than losing the turn to an exception.
        """
        ...


__all__ = [
    "OUTCOME_CLAIM_CONSISTENT",
    "OUTCOME_CLAIM_INCONSISTENT",
    "OUTCOME_CLAIM_UNAVAILABLE",
    "OutcomeClaimEvidence",
    "OutcomeClaimJudgePort",
    "OutcomeClaimVerdict",
]
