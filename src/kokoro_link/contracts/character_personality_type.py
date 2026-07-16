"""Ports for 16 型性格建議與一致性分析."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)


@dataclass(frozen=True, slots=True)
class CharacterPersonalityTypeAnalysisInput:
    name: str = ""
    summary: str = ""
    personality: tuple[str, ...] = ()
    interests: tuple[str, ...] = ()
    speaking_style: str = ""
    boundaries: tuple[str, ...] = ()
    aspirations: tuple[str, ...] = ()
    user_selected_type: CharacterPersonalityType | None = None
    current_type: CharacterPersonalityType | None = None
    operator_primary_language: str = "zh-TW"


@dataclass(frozen=True, slots=True)
class CharacterPersonalityTypeAnalysis:
    suggested_type: CharacterPersonalityType = field(
        default_factory=lambda: CharacterPersonalityType.DEFAULT,  # type: ignore[attr-defined]
    )
    is_consistent: bool = True
    conflict_level: str = "none"
    conflict_notes: tuple[str, ...] = ()
    user_questions: tuple[str, ...] = ()

    @property
    def is_blocking(self) -> bool:
        return self.conflict_level == "blocking"


class CharacterPersonalityTypeAnalyzerPort(Protocol):
    async def analyze(
        self,
        request: CharacterPersonalityTypeAnalysisInput,
    ) -> CharacterPersonalityTypeAnalysis:
        """Return an LLM-backed suggestion / consistency check."""
