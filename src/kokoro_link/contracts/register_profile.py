"""Ports and DTOs for per-turn reply register profiling."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from kokoro_link.domain.entities.character import Character


_AXIS_NAMES: tuple[str, ...] = (
    "emotional_intensity",
    "seriousness",
    "intimacy",
    "humor_latitude",
    "help_seeking",
)


@dataclass(frozen=True, slots=True)
class RegisterProfileContext:
    character_id: str
    operator_id: str
    latest_user_message: str
    recent_dialogue_summary: str = ""
    relationship_context: tuple[str, ...] = ()
    content_tolerance: str = "frontier"


@dataclass(frozen=True, slots=True)
class RegisterProfile:
    axes: dict[str, float] = field(default_factory=dict)
    confidence: float = 0.0
    note: str = ""
    vulnerable_disclosure: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def axis(self, name: str, default: float = 0.0) -> float:
        value = self.axes.get(name, default)
        return _clamp01(value)

    @property
    def emotional_intensity(self) -> float:
        return self.axis("emotional_intensity")

    @property
    def seriousness(self) -> float:
        return self.axis("seriousness")

    @property
    def intimacy(self) -> float:
        return self.axis("intimacy")

    @property
    def humor_latitude(self) -> float:
        return self.axis("humor_latitude")

    @property
    def help_seeking(self) -> float:
        return self.axis("help_seeking")

    @classmethod
    def neutral(cls, reason: str = "") -> "RegisterProfile":
        return cls(
            axes={name: 0.0 for name in _AXIS_NAMES},
            confidence=0.0,
            note=reason,
            vulnerable_disclosure=False,
            metadata={"fallback": True, "reason": reason} if reason else {"fallback": True},
        )


class RegisterProfilePort(Protocol):
    async def profile(
        self,
        context: RegisterProfileContext,
        *,
        character: Character | None = None,
    ) -> RegisterProfile | None:
        """Return a semantic register profile or ``None`` for neutral fail-soft."""


def normalise_axes(raw: object) -> dict[str, float]:
    if not isinstance(raw, dict):
        return {name: 0.0 for name in _AXIS_NAMES}
    return {
        name: _clamp01(raw.get(name, 0.0))
        for name in _AXIS_NAMES
    }


def _clamp01(value: object) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number < 0.0:
        return 0.0
    if number > 1.0:
        return 1.0
    return number
