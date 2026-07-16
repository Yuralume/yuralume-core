"""Ports for the emotion event-sourcing pipeline.

* ``EmotionEventRepositoryPort`` — append-only event store keyed on
  ``(character_id, operator_id)``. List queries are time-windowed so
  the aggregator never scans the full history.
* ``EmotionAggregatorPort`` — pure function from event list + now →
  derived snapshot. Kept as a port so dream / disposition-drift can
  swap in alternative aggregation policies (e.g. seasonal weighting)
  without rewriting the chat path.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.emotion_event import EmotionEvent


@dataclass(frozen=True, slots=True)
class EmotionSnapshot:
    """Derived view over recent emotion events.

    Aggregator-side computation: deltas integrated with decay weights,
    clamped to the same [0, 100] range ``CharacterState`` uses. The
    ``emotion`` string comes from the most recent ``cause_ref_kind=turn``
    event so the prompt can show "懊惱" rather than re-deriving from
    numbers. ``top_events`` is the prompt-ready ranked list — the chat /
    proactive / planner prompts inject it verbatim so the LLM can ground
    its tone in concrete moments.
    """
    emotion: str
    affection: int
    fatigue: int
    trust: int
    energy: int
    valence: float
    arousal: float
    top_events: tuple[EmotionEvent, ...]


class EmotionEventRepositoryPort(Protocol):
    async def add(self, event: EmotionEvent) -> None: ...

    async def add_many(self, events: list[EmotionEvent]) -> None: ...

    async def list_recent(
        self,
        *,
        character_id: str,
        operator_id: str,
        since: datetime,
        limit: int = 100,
    ) -> list[EmotionEvent]: ...

    async def delete_for_character(
        self, character_id: str,
    ) -> int: ...


class EmotionAggregatorPort(Protocol):
    def derive(
        self,
        *,
        events: list[EmotionEvent],
        baseline_affection: int,
        baseline_fatigue: int,
        baseline_trust: int,
        baseline_energy: int,
        baseline_emotion: str,
        now: datetime,
        top_k: int = 5,
    ) -> EmotionSnapshot:
        """Fold ``events`` into a derived snapshot.

        ``baseline_*`` is the persisted ``CharacterState`` from before
        any of the supplied events applied. Aggregator integrates each
        event's deltas weighted by exponential decay from
        ``event.created_at`` to ``now``, then clamps to [0, 100].
        ``top_k`` controls the size of ``EmotionSnapshot.top_events``
        (ranked by decayed intensity).
        """
        ...
