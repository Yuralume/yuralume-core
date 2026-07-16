"""Ports for the disposition-drift pipeline (HUMANIZATION_ROADMAP §3.1)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.disposition_drift_record import (
    DispositionDriftRecord,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.disposition import CharacterDisposition


@dataclass(frozen=True, slots=True)
class DispositionDriftProposal:
    """One nudge the LLM suggested for a dimension.

    ``direction`` is ``"up"`` / ``"down"`` / ``"none"``. The service applies
    a single-band shift only — if the current band is already at the
    extreme (``high`` + ``up``, ``low`` + ``down``), the proposal is
    rejected to enforce the "max 1 band per pass" constraint.
    """

    dimension: str
    direction: str
    reason: str
    evidence_quote: str = ""


@dataclass(frozen=True, slots=True)
class DispositionDriftInput:
    character_id: str
    character_name: str
    disposition: CharacterDisposition
    emotion_event_summary: str
    high_salience_memories: tuple[MemoryItem, ...]
    window_days: int = 30
    persona_summary_lines: tuple[str, ...] = field(default_factory=tuple)


class DispositionDriftJudgePort(Protocol):
    async def judge(
        self, payload: DispositionDriftInput,
    ) -> DispositionDriftProposal | None:
        """Decide whether any single dimension should nudge this pass.

        Returns ``None`` when the LLM declines (no clear signal, evidence
        too thin, dimension at extreme). The service then writes no
        history row and the character stays put."""


class NullDispositionDriftJudge(DispositionDriftJudgePort):
    async def judge(
        self, payload: DispositionDriftInput,
    ) -> DispositionDriftProposal | None:
        return None


class DispositionDriftHistoryRepositoryPort(Protocol):
    async def add(
        self, record: DispositionDriftRecord,
    ) -> DispositionDriftRecord:
        """Append one audit row."""

    async def list_for_character(
        self,
        character_id: str,
        *,
        limit: int = 20,
    ) -> list[DispositionDriftRecord]:
        """Return audit rows newest first. Powers the 人格演化軌跡 admin view."""

    async def latest_for_dimension(
        self,
        character_id: str,
        dimension: str,
    ) -> DispositionDriftRecord | None:
        """Return the most recent record for ``(character, dimension)``.
        Used to enforce the 30-day cooldown — a fresh shift must wait
        the full cooldown after the previous one for the same dimension."""

    async def delete_for_character(self, character_id: str) -> int:
        """Cascade purge for the operator-purge CLI."""
