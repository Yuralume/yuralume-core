"""Outcome of a proactive evaluation — serves as the audit log tag."""

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
ProactiveOutcome.SENT = ProactiveOutcome("sent")
ProactiveOutcome.ERRORED = ProactiveOutcome("errored")
