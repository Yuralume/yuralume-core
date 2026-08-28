"""Outcome of a proactive evaluation — serves as the audit log tag.

Persisted as a plain ``String(32)`` (``proactive_attempts.outcome``) with
no DB-side enum or check constraint, so a **new** member is a code-only
change: old rows keep their value, and a reader that has never heard of
the new one still round-trips it through :meth:`ProactiveOutcome.from_string`.
Renaming or removing one is not — those values are in the operator's
history table.
"""

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True, slots=True)
class ProactiveOutcome:
    value: str

    DISABLED: "ClassVar[ProactiveOutcome]"        # character.proactive_enabled is False
    GATE_BLOCKED: "ClassVar[ProactiveOutcome]"    # heuristic gate dropped it
    NO_BINDING: "ClassVar[ProactiveOutcome]"      # no eligible channel binding
    INTENTION_SKIPPED: "ClassVar[ProactiveOutcome]" # LLM intention judge said "not now"
    DECIDER_SKIPPED: "ClassVar[ProactiveOutcome]" # LLM said "don't send"
    QUALITY_WITHHELD: "ClassVar[ProactiveOutcome]" # output-quality gate withheld a broken draft
    SLOT_TAKEN: "ClassVar[ProactiveOutcome]"      # another runner owns this tick slot (P3-Dedup)
    SENT: "ClassVar[ProactiveOutcome]"            # message pushed to platform
    ERRORED: "ClassVar[ProactiveOutcome]"         # unexpected failure

    def __post_init__(self) -> None:
        if not self.value or not self.value.strip():
            raise ValueError("ProactiveOutcome value must be non-empty")
        object.__setattr__(self, "value", self.value.strip().lower())

    def __str__(self) -> str:
        return self.value

    @classmethod
    def from_string(cls, raw: str) -> "ProactiveOutcome":
        return cls(raw)


ProactiveOutcome.DISABLED = ProactiveOutcome("disabled")
ProactiveOutcome.GATE_BLOCKED = ProactiveOutcome("gate_blocked")
ProactiveOutcome.NO_BINDING = ProactiveOutcome("no_binding")
ProactiveOutcome.INTENTION_SKIPPED = ProactiveOutcome("intention_skipped")
ProactiveOutcome.DECIDER_SKIPPED = ProactiveOutcome("decider_skipped")
#: The character *wanted* to speak and the output-quality gate refused the
#: draft it wrote. Deliberately not ``DECIDER_SKIPPED``: that one means "the
#: character chose silence", which is an authentic beat the cooldown should
#: honour, whereas this one is a machine refusing broken prose. Telling them
#: apart is what lets the cooldown anchor skip this row (see
#: ``ProactiveAttemptRepositoryPort.latest_passing_gate_for_character``) —
#: one quality failure must not silence the whole cooldown window.
ProactiveOutcome.QUALITY_WITHHELD = ProactiveOutcome("quality_withheld")
ProactiveOutcome.SLOT_TAKEN = ProactiveOutcome("slot_taken")
ProactiveOutcome.SENT = ProactiveOutcome("sent")
ProactiveOutcome.ERRORED = ProactiveOutcome("errored")

NON_COOLDOWN_ANCHOR_OUTCOMES: frozenset[ProactiveOutcome] = frozenset({
    ProactiveOutcome.DISABLED,
    ProactiveOutcome.GATE_BLOCKED,
    ProactiveOutcome.QUALITY_WITHHELD,
})
"""Outcomes a cooldown must not anchor on — two reasons, one treatment.

``DISABLED`` / ``GATE_BLOCKED`` were stopped before any expensive work, so
anchoring on them would re-block the next tick and the cooldown would never
lapse in practice.

``QUALITY_WITHHELD`` did spend the budget, but the player got **nothing**:
the output-quality gate refused a broken draft. Anchoring on it would let one
quality failure silence the entire cooldown window — the opposite of the
"skip this tick, retry naturally" semantics a withhold is supposed to have.

A decider's own ``DECIDER_SKIPPED`` is absent on purpose. "The character
chose not to message" is an authentic beat; re-asking every tick would both
burn budget and override its decision.

Lives here rather than in either repository because both of them answer
:meth:`ProactiveAttemptRepositoryPort.latest_passing_gate_for_character` and
two copies of this list is exactly how the in-memory and SQL cooldowns drift
apart without anyone noticing.
"""
