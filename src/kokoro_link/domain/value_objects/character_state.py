from dataclasses import dataclass, replace
from datetime import datetime


def _clamp(value: int) -> int:
    return max(0, min(100, value))


_UNSET = object()


@dataclass(frozen=True, slots=True)
class CharacterState:
    emotion: str
    affection: int
    fatigue: int
    trust: int
    energy: int
    last_active_at: datetime | None = None
    current_intent: str | None = None
    """Short-term goal for the current conversation (1-sentence, revised each turn)."""

    def adjust(
        self,
        *,
        emotion: str | None = None,
        affection_delta: int = 0,
        fatigue_delta: int = 0,
        trust_delta: int = 0,
        energy_delta: int = 0,
        current_intent: str | None | object = _UNSET,
    ) -> "CharacterState":
        next_intent = self.current_intent if current_intent is _UNSET else current_intent
        return replace(
            self,
            emotion=self.emotion if emotion is None else emotion,
            affection=_clamp(self.affection + affection_delta),
            fatigue=_clamp(self.fatigue + fatigue_delta),
            trust=_clamp(self.trust + trust_delta),
            energy=_clamp(self.energy + energy_delta),
            current_intent=next_intent,
        )

    def with_active_now(self, now: datetime) -> "CharacterState":
        """Return a copy with ``last_active_at`` set to *now*."""
        return replace(self, last_active_at=now)
