"""Per-cause emotion event — event-sourcing layer for character mood.

Goal: replace the ad-hoc ``CharacterState.adjust(affection_delta=...)``
deltas with an append-only event log so we can audit *why* the character
feels what they feel right now and run analysis (cause × valence ×
intensity over time) the dashboard / dream service / disposition drift
all want.

Each event carries:

* ``cause_ref_kind`` + ``cause_ref_id`` — polymorphic foreign-key style
  the same way :class:`FeedSource` ties feed posts back to their source
  subsystem. Open set of kinds (chat turn / idle drift / rest recovery
  / proactive attempt / world event / dream) so new subsystems can plug
  in without touching domain code.
* deltas (``affection_delta`` / ``fatigue_delta`` / ``trust_delta`` /
  ``energy_delta``) — same signed integer space as the current
  ``CharacterState.adjust`` API. Integration via :class:`EmotionAggregator`
  applies an exponential decay so old events fade.
* ``valence`` / ``arousal`` / ``intensity`` — continuous dimensions for
  the prompt-side "recent 24h emotion summary" the LLM gets handed.
* ``emotion_label`` + ``evidence_quote`` — free text so the LLM can
  surface "why" without us hard-coding categories.
* ``decay_half_life_minutes`` — how fast this event's influence fades.
  Idle drift fades in hours; a betrayal might fade over weeks.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Final
from uuid import uuid4


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# Open set — adapters / services pass strings, no enum gatekeeping.
CAUSE_TURN: Final = "turn"
CAUSE_IDLE_DRIFT: Final = "idle_drift"
CAUSE_REST_RECOVERY: Final = "rest_recovery"
CAUSE_PROACTIVE_ATTEMPT: Final = "proactive_attempt"
CAUSE_WORLD_EVENT: Final = "world_event"
CAUSE_DREAM: Final = "dream"


@dataclass(frozen=True, slots=True)
class EmotionEvent:
    id: str
    character_id: str
    operator_id: str
    """Per-operator scope. Same isolation rule as ``OperatorPersona`` —
    events from one (character, operator) pair never bleed into
    another. Defaults to the system / default operator id when the
    cause has no operator (idle drift, rest recovery)."""
    cause_ref_kind: str
    """One of CAUSE_* (open set). Names the subsystem that produced
    this event. Used by the aggregator to weight (e.g. dream events
    integrate slower than turn events) and by the dashboard to slice."""
    cause_ref_id: str | None = None
    """ID of the source row (turn_record.id, proactive_attempt.id, ...).
    ``None`` when the cause has no addressable source (e.g. idle drift
    runs are batched and not individually addressable)."""

    # Continuous emotional dimensions — for prompt-side summarisation.
    valence: float = 0.0
    """-1.0 = strongly negative, 0.0 = neutral, +1.0 = strongly positive."""
    arousal: float = 0.0
    """0.0 = calm / quiet, 1.0 = highly activated. Distinct from
    valence — both anger and joy are high-arousal."""
    intensity: float = 0.5
    """0.0 to 1.0 — overall salience; how big a deal this event is.
    Used to rank the top-N for prompt injection."""

    # Discrete deltas — applied to ``CharacterState`` numeric fields.
    affection_delta: int = 0
    fatigue_delta: int = 0
    trust_delta: int = 0
    energy_delta: int = 0
    applied_to_state: bool = False
    """Compatibility marker for the flat ``CharacterState`` columns.

    ``True`` means this event's numeric deltas have already been applied
    to the persisted state columns by the legacy path (rest recovery,
    idle drift, older post-turn rows). The aggregator still uses the
    event for labels / valence / top-events, but skips numeric deltas to
    avoid double-counting when projecting a read model.
    """

    emotion_label: str = ""
    """Free-text LLM-produced label (e.g. ``"被理解了"`` / ``"懊惱"``).
    Replaces the ``CharacterState.emotion`` string verbatim when this
    is the most recent ``cause_ref_kind=turn`` event."""
    evidence_quote: str = ""
    """Short quote (≤ ~120 chars) from the conversation / world event
    that justifies this emotion event. Shown to the LLM in the prompt
    "最近 24h 情緒事件" block so it can ground its tone in concrete
    moments rather than abstract numbers."""

    decay_half_life_minutes: int = 240
    """How fast this event's influence fades. 240 (4 hours) by default
    — matches the existing rest-recovery half-life. Aggregator computes
    ``weight = 2 ** (-elapsed_minutes / half_life)`` and multiplies
    deltas / intensity / valence by it."""
    expires_at: datetime | None = None
    """Hard cutoff after which the event is treated as fully decayed
    regardless of half-life. ``None`` = honour half-life only. Useful
    for "this just lasts until tomorrow morning" events."""
    created_at: datetime = field(default_factory=_utcnow)

    @classmethod
    def new(
        cls,
        *,
        character_id: str,
        operator_id: str,
        cause_ref_kind: str,
        cause_ref_id: str | None = None,
        valence: float = 0.0,
        arousal: float = 0.0,
        intensity: float = 0.5,
        affection_delta: int = 0,
        fatigue_delta: int = 0,
        trust_delta: int = 0,
        energy_delta: int = 0,
        applied_to_state: bool = False,
        emotion_label: str = "",
        evidence_quote: str = "",
        decay_half_life_minutes: int = 240,
        expires_at: datetime | None = None,
        now: datetime | None = None,
    ) -> EmotionEvent:
        return cls(
            id=str(uuid4()),
            character_id=character_id,
            operator_id=operator_id,
            cause_ref_kind=cause_ref_kind,
            cause_ref_id=cause_ref_id,
            valence=_clamp_signed(valence),
            arousal=_clamp_unit(arousal),
            intensity=_clamp_unit(intensity),
            affection_delta=int(affection_delta),
            fatigue_delta=int(fatigue_delta),
            trust_delta=int(trust_delta),
            energy_delta=int(energy_delta),
            applied_to_state=bool(applied_to_state),
            emotion_label=emotion_label.strip(),
            evidence_quote=evidence_quote.strip(),
            decay_half_life_minutes=max(1, int(decay_half_life_minutes)),
            expires_at=expires_at,
            created_at=now or _utcnow(),
        )


def _clamp_signed(value: float) -> float:
    return max(-1.0, min(1.0, float(value)))


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
