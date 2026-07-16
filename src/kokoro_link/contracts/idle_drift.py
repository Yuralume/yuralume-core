"""Idle-drift port.

When the user hasn't chatted in a while, the character's mood should
shift in a personality-appropriate way: a tsundere sulks (鬧彆扭), a
clingy character feels neglected (失落), a cool character barely
notices, an anxious one worries something happened. The shape of the
drift is decided by an LLM reading the persona axes — never by
keyword enumeration.

The port is intentionally narrow: input is the character + how long
they've been left alone; output is an optional emotion override plus
small numeric nudges to affection/fatigue/energy. Callers fold the
drift into the pre-turn ``pending_state`` so the next reply naturally
reflects the new mood without any explicit instruction in the prompt.

Empty output means "no notable drift — proceed normally". This is the
correct response when the absence is short or when the persona is one
that doesn't really care about idle gaps. Returning empty is a feature,
not a failure mode: forcing every long absence into "sad" or "annoyed"
would flatten personality differences.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from kokoro_link.domain.entities.character import Character


@dataclass(frozen=True, slots=True)
class IdleDrift:
    emotion: str | None = None
    """Optional emotion override (e.g. ``"鬧彆扭"``). ``None`` keeps the
    current emotion — pair with non-zero deltas to nudge stats without
    changing the named mood."""

    affection_delta: int = 0
    fatigue_delta: int = 0
    energy_delta: int = 0
    current_intent: str | None = None
    """Optional revised short-term intent (e.g. ``"裝沒事但其實有點在意"``).
    Lets the LLM script the inner stance for the next few turns."""

    note: str = ""
    """One-line internal residue (e.g. ``"三天沒消息，有點失落"``). Not
    surfaced in the prompt directly — kept for logging and for future
    use cases that want the human-readable judgement alongside the
    numeric drift."""

    @property
    def is_empty(self) -> bool:
        return (
            self.emotion is None
            and self.affection_delta == 0
            and self.fatigue_delta == 0
            and self.energy_delta == 0
            and self.current_intent is None
            and not self.note.strip()
        )


class IdleDriftPort(Protocol):
    async def judge(
        self,
        *,
        character: Character,
        idle_minutes: float,
        operator_primary_language: str = "zh-TW",
    ) -> IdleDrift:
        """Decide how the character's mood has drifted during the
        ``idle_minutes`` since the user's last message. Implementations
        must never raise to the caller — fail-soft by returning an
        empty :class:`IdleDrift`.

        ``operator_primary_language`` is the operator's content language
        (BCP 47). ``current_intent`` is player-visible, so the judge must
        emit it in this language rather than a hardcoded one. Defaults to
        ``zh-TW`` for legacy callers."""
