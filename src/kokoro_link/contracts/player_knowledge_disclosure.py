"""Disclosure judge port (KB8, proactive channel).

The question is narrow: **of the memories the player has never been told
that went into composing this outbound message, which ones does the
message actually tell him?**

Chat gets this answer for free — its post-turn pass already re-reads the
turn, so one more section on an existing call suffices. A proactive push
has no post-turn pass: it is composed, delivered, and done. So the same
question needs its own small call, and D10 chose to pay for it rather
than take the cheap answer.

The cheap answer would have been "the material went into the prompt,
therefore it was disclosed". That is wrong in the direction that hurts.
The decider is handed several memories as context and writes one short
message; most of them shape the tone and never surface as content. Under
"injected ⇒ disclosed" every one of them is marked told, and the
character then refers to things the player has never heard of as shared
ground — which is precisely the 2026-08-25 incident this plan exists to
prevent.

What the judge is given, and what it is not
-------------------------------------------
Given: the delivered message text, and the candidate memories as
``(id, content)`` pairs.

Not given: persona, relationship, schedule, the reasoning that produced
the message. Same red line as the honesty judge (HV1) and for the same
reason: a judge that knows why she wrote the message starts inferring
what she must have meant, and "she was clearly alluding to it" is not
the same fact as "she told him". Only the words that reached the player
count.

Fail direction
--------------
:meth:`DisclosureVerdict.failed` is a third answer, never folded into
"nothing was disclosed" — even though both flip nothing today. The
distinction is for the caller's logs: a run of failures is an outage
worth seeing, while a run of empty verdicts is the ordinary case. Both
leave every candidate ``private``, which is the safe direction: the cost
is the character introducing something a second time, against the cost
of her treating an untold fact as common ground.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kokoro_link.domain.entities.character import Character


@dataclass(frozen=True, slots=True)
class DisclosureCandidate:
    """One ``private`` memory that went into composing this message.

    A flat ``(id, content)`` pair rather than the ``MemoryItem`` itself:
    the judge has no business with salience, participants or embeddings,
    and a port that cannot see them cannot start ranking by them.
    """

    memory_id: str
    content: str


@dataclass(frozen=True, slots=True)
class DisclosureVerdict:
    """Which candidates the message told the player about."""

    disclosed_ids: tuple[str, ...] = ()
    unavailable: bool = False
    """No verdict could be produced — call failed, reply unparseable, or
    the answer was the wrong shape. Never means "nothing was disclosed";
    see the module docstring."""

    @classmethod
    def none(cls) -> "DisclosureVerdict":
        """A real verdict of "she mentioned none of them"."""
        return cls()

    @classmethod
    def of(cls, ids: tuple[str, ...]) -> "DisclosureVerdict":
        return cls(disclosed_ids=ids)

    @classmethod
    def failed(cls) -> "DisclosureVerdict":
        return cls(unavailable=True)


class PlayerKnowledgeDisclosureJudgePort(Protocol):
    async def judge(
        self,
        *,
        message_text: str,
        candidates: tuple[DisclosureCandidate, ...],
        character: Character | None = None,
    ) -> DisclosureVerdict:
        """Read the delivered message against the candidates.

        ``character`` routes the call (per-character model overrides) and
        never reaches the prompt — see the red line above.

        Implementations must return only ids drawn from ``candidates``.
        The caller intersects anyway, because a ledger this hard to walk
        back does not rely on a collaborator keeping its promises.
        """


__all__ = [
    "DisclosureCandidate",
    "DisclosureVerdict",
    "PlayerKnowledgeDisclosureJudgePort",
]
