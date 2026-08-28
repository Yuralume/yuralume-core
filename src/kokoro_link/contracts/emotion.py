"""Ports for the emotion event-sourcing pipeline.

* ``EmotionEventRepositoryPort`` — append-only event store keyed on
  ``(character_id, operator_id)``. List queries are time-windowed so
  the aggregator never scans the full history; the one *removal* the
  store offers besides character erasure is keyed on the cause instead,
  because reversing a turn has to hit that turn's rows and no others.
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

    async def delete_by_cause(
        self,
        *,
        character_id: str,
        cause_ref_kind: str,
        cause_ref_id: str,
    ) -> int:
        """Delete every event this one cause produced. Returns the count.

        Keyed on the cause, never on a time window. The producer of
        ``cause_ref_kind="turn"`` events is the *background* post-turn,
        so a window anchored on when the turn started either misses the
        events that land after it closes or swallows a neighbouring
        turn's — and undo has exactly one job that a near-miss ruins.
        The cause reference is the identity the writer already stamped,
        so it is the identity the reverser reads.

        ``character_id`` is part of the key for two reasons: it is the
        indexed column on the table, which keeps the delete off a full
        scan; and it confines an undo to the character whose turn is
        being reversed, so a stray reference can never reach across.

        Idempotent by construction — a second call finds nothing and
        returns ``0``. Implementations return ``0`` rather than raising
        when any part of the key is empty: a missing anchor means there
        is nothing addressable to delete, not an error.
        """
        ...

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
