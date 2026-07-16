"""Ports for the self-reflection pipeline (HUMANIZATION_ROADMAP §3.2)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.self_reflection import SelfReflection


@dataclass(frozen=True, slots=True)
class ReflectionGeneratorInput:
    """Bundle handed to the LLM generator.

    Pure data — no service references — so the generator can be tested
    in isolation. The dream-pass service is responsible for slicing
    memories / emotion events into the right window before constructing
    this.
    """

    character_id: str
    operator_id: str
    character_name: str
    period: str
    period_start: date
    period_end: date
    high_salience_memories: tuple[MemoryItem, ...]
    operator_primary_language: str = "zh-TW"
    emotion_event_summary: str = ""
    """Pre-rendered text describing the dominant valence / arousal /
    cause mix from the EmotionEvent log (HUMANIZATION_ROADMAP §2.2).
    Empty string skips the section."""
    persona_summary_lines: tuple[str, ...] = field(default_factory=tuple)
    """Optional thresholded persona snippets so the reflection knows
    *who* the operator is to this character (per [[per-character-isolation]])."""


class SelfReflectionGeneratorPort(Protocol):
    async def generate(
        self, payload: ReflectionGeneratorInput,
    ) -> SelfReflection | None:
        """Produce a fresh ``SelfReflection`` for the given window.

        Returns ``None`` when the LLM produces nothing usable (empty
        narrative, parse failure, fake provider). Callers treat ``None``
        as "no update this pass" — older rows from prior passes still
        anchor the prompt block via the repository's read path.
        """


class SelfReflectionRepositoryPort(Protocol):
    async def upsert_latest(
        self, reflection: SelfReflection,
    ) -> SelfReflection:
        """Replace the previous (character, operator, period) row with
        ``reflection``. Period rows always have at most one current
        snapshot — historical archives belong in a future
        ``reflection_history`` surface (out of scope today)."""

    async def latest_for(
        self, character_id: str, operator_id: str,
    ) -> list[SelfReflection]:
        """Return at most two rows (one per period) for the pair, newest
        first. Empty when no reflection has ever been generated."""

    async def delete_for_character(self, character_id: str) -> int:
        """Wipe every reflection row for a character."""


class NullSelfReflectionGenerator(SelfReflectionGeneratorPort):
    """Pass-through generator used when the feature is intentionally
    disabled or the provider is fake."""

    async def generate(
        self, payload: ReflectionGeneratorInput,
    ) -> SelfReflection | None:
        return None
