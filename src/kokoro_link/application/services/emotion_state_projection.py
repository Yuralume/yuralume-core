"""Project a display/read CharacterState from recent EmotionEvents."""

from __future__ import annotations

from kokoro_link.contracts.emotion import EmotionAggregatorPort
from kokoro_link.domain.entities.emotion_event import EmotionEvent
from kokoro_link.domain.value_objects.character_state import CharacterState


def project_state_from_emotion_events(
    *,
    state: CharacterState,
    events: list[EmotionEvent],
    aggregator: EmotionAggregatorPort,
    now,
) -> CharacterState:
    """Return the aggregator-authoritative read model for state.

    The persisted flat columns are the compatibility baseline. Events
    whose numeric deltas were already applied to those columns carry
    ``applied_to_state=True``; the aggregator skips their numeric deltas
    but still uses them for labels / valence / top-events.
    """
    if not events:
        return state
    snapshot = aggregator.derive(
        events=events,
        baseline_affection=state.affection,
        baseline_fatigue=state.fatigue,
        baseline_trust=state.trust,
        baseline_energy=state.energy,
        baseline_emotion=state.emotion,
        now=now,
    )
    return CharacterState(
        emotion=snapshot.emotion,
        affection=snapshot.affection,
        fatigue=snapshot.fatigue,
        trust=snapshot.trust,
        energy=snapshot.energy,
        last_active_at=state.last_active_at,
        current_intent=state.current_intent,
    )
