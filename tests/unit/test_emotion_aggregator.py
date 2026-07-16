"""Tests for ExponentialDecayEmotionAggregator.

The aggregator is pure math — it's where any subtle bug in the
event-sourcing pipeline would hide. Cover:

1. No events → returns baseline verbatim (no drift).
2. Single fresh event → deltas applied at full weight.
3. Single old event (1 half-life past) → deltas applied at ~50% weight.
4. Expired event → contributes nothing.
5. Clamping to [0, 100] on extreme deltas.
6. ``emotion`` label sourced from latest ``cause_ref_kind=turn`` event.
7. ``top_events`` ranked by intensity × weight.
8. Valence / arousal weighted-mean math.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.emotion_aggregator import (
    ExponentialDecayEmotionAggregator,
)
from kokoro_link.domain.entities.emotion_event import (
    CAUSE_IDLE_DRIFT,
    CAUSE_REST_RECOVERY,
    CAUSE_TURN,
    EmotionEvent,
)


def _at(minutes_ago: int) -> datetime:
    return _NOW - timedelta(minutes=minutes_ago)


_NOW = datetime(2026, 5, 20, 12, 0, tzinfo=timezone.utc)


def _evt(**kwargs) -> EmotionEvent:
    defaults = dict(
        character_id="c",
        operator_id="op",
        cause_ref_kind=CAUSE_TURN,
        now=_NOW,
    )
    defaults.update(kwargs)
    return EmotionEvent.new(**defaults)


def _agg():
    return ExponentialDecayEmotionAggregator()


def test_no_events_returns_baseline():
    snapshot = _agg().derive(
        events=[],
        baseline_affection=50,
        baseline_fatigue=30,
        baseline_trust=60,
        baseline_energy=80,
        baseline_emotion="neutral",
        now=_NOW,
    )
    assert snapshot.affection == 50
    assert snapshot.fatigue == 30
    assert snapshot.trust == 60
    assert snapshot.energy == 80
    assert snapshot.emotion == "neutral"
    assert snapshot.top_events == ()


def test_fresh_event_applies_full_weight():
    event = EmotionEvent.new(
        character_id="c", operator_id="op",
        cause_ref_kind=CAUSE_TURN,
        affection_delta=10,
        fatigue_delta=-5,
        emotion_label="開心",
        now=_NOW,
    )
    snapshot = _agg().derive(
        events=[event],
        baseline_affection=50, baseline_fatigue=30,
        baseline_trust=60, baseline_energy=80,
        baseline_emotion="neutral",
        now=_NOW,
    )
    # Fresh: weight ~= 1.0; full delta applied.
    assert snapshot.affection == 60
    assert snapshot.fatigue == 25
    assert snapshot.emotion == "開心"


def test_one_half_life_old_event_applies_half_weight():
    event = EmotionEvent(
        id="e1",
        character_id="c", operator_id="op",
        cause_ref_kind=CAUSE_TURN,
        affection_delta=20,
        intensity=1.0,
        decay_half_life_minutes=60,
        created_at=_at(60),  # exactly one half-life ago
    )
    snapshot = _agg().derive(
        events=[event],
        baseline_affection=50, baseline_fatigue=30,
        baseline_trust=60, baseline_energy=80,
        baseline_emotion="neutral",
        now=_NOW,
    )
    # weight = 0.5 → delta 20 × 0.5 = 10 → affection 60.
    assert snapshot.affection == 60


def test_expired_event_contributes_nothing():
    event = EmotionEvent(
        id="e1",
        character_id="c", operator_id="op",
        cause_ref_kind=CAUSE_TURN,
        affection_delta=50,
        intensity=1.0,
        decay_half_life_minutes=1000,  # would still be heavy
        expires_at=_at(10),  # but explicitly expired 10 min ago
        created_at=_at(120),
    )
    snapshot = _agg().derive(
        events=[event],
        baseline_affection=50, baseline_fatigue=30,
        baseline_trust=60, baseline_energy=80,
        baseline_emotion="neutral",
        now=_NOW,
    )
    assert snapshot.affection == 50  # unchanged


def test_clamps_to_zero_and_hundred():
    big_drop = _evt(affection_delta=-200, energy_delta=-200)
    big_lift = _evt(trust_delta=200, fatigue_delta=200)
    snapshot = _agg().derive(
        events=[big_drop, big_lift],
        baseline_affection=10, baseline_fatigue=10,
        baseline_trust=10, baseline_energy=10,
        baseline_emotion="neutral",
        now=_NOW,
    )
    assert snapshot.affection == 0
    assert snapshot.energy == 0
    assert snapshot.trust == 100
    assert snapshot.fatigue == 100


def test_emotion_label_uses_latest_turn_event():
    older_turn = EmotionEvent(
        id="e1", character_id="c", operator_id="op",
        cause_ref_kind=CAUSE_TURN,
        emotion_label="冷靜",
        created_at=_at(30),
    )
    newer_turn = EmotionEvent(
        id="e2", character_id="c", operator_id="op",
        cause_ref_kind=CAUSE_TURN,
        emotion_label="興奮",
        created_at=_at(5),
    )
    # Non-turn drift event must NOT win even if newer.
    drift = EmotionEvent(
        id="e3", character_id="c", operator_id="op",
        cause_ref_kind=CAUSE_IDLE_DRIFT,
        emotion_label="放空",
        created_at=_at(1),
    )
    snapshot = _agg().derive(
        events=[older_turn, newer_turn, drift],
        baseline_affection=50, baseline_fatigue=30,
        baseline_trust=60, baseline_energy=80,
        baseline_emotion="neutral",
        now=_NOW,
    )
    assert snapshot.emotion == "興奮"


def test_top_events_ranked_by_decayed_intensity():
    fresh_low = _evt(intensity=0.4, emotion_label="A")
    older_high = EmotionEvent(
        id="e2", character_id="c", operator_id="op",
        cause_ref_kind=CAUSE_TURN,
        intensity=0.9,
        emotion_label="B",
        created_at=_at(60),  # decayed to 0.5 weight
        decay_half_life_minutes=60,
    )
    snapshot = _agg().derive(
        events=[fresh_low, older_high],
        baseline_affection=50, baseline_fatigue=30,
        baseline_trust=60, baseline_energy=80,
        baseline_emotion="neutral",
        now=_NOW,
        top_k=2,
    )
    # 0.4 × ~1.0 = 0.4 vs 0.9 × 0.5 = 0.45 → older_high ranks higher.
    assert snapshot.top_events[0].emotion_label == "B"
    assert snapshot.top_events[1].emotion_label == "A"


def test_valence_arousal_weighted_mean():
    events = [
        _evt(valence=1.0, arousal=0.8, intensity=1.0),
        _evt(valence=-1.0, arousal=0.2, intensity=1.0),
    ]
    snapshot = _agg().derive(
        events=events,
        baseline_affection=50, baseline_fatigue=30,
        baseline_trust=60, baseline_energy=80,
        baseline_emotion="neutral",
        now=_NOW,
    )
    # Equal intensity opposite valence → near zero.
    assert -0.05 < snapshot.valence < 0.05
    # Arousal weighted mean ≈ 0.5.
    assert 0.45 < snapshot.arousal < 0.55


def test_rest_recovery_event_can_lift_energy():
    """Rest recovery as an event (Phase 3.4 will migrate the implementation
    to emit these). This test pins the contract: such an event lifts
    energy / lowers fatigue when integrated."""
    rest = _evt(
        cause_ref_kind=CAUSE_REST_RECOVERY,
        fatigue_delta=-40,
        energy_delta=+40,
        decay_half_life_minutes=240,
    )
    snapshot = _agg().derive(
        events=[rest],
        baseline_affection=50, baseline_fatigue=90,
        baseline_trust=60, baseline_energy=10,
        baseline_emotion="exhausted",
        now=_NOW,
    )
    assert snapshot.fatigue == 50
    assert snapshot.energy == 50


def test_column_applied_event_does_not_double_count_numeric_deltas():
    event = _evt(
        emotion_label="安心",
        affection_delta=10,
        fatigue_delta=10,
        applied_to_state=True,
    )
    snapshot = _agg().derive(
        events=[event],
        baseline_affection=60, baseline_fatigue=40,
        baseline_trust=60, baseline_energy=80,
        baseline_emotion="neutral",
        now=_NOW,
    )
    assert snapshot.affection == 60
    assert snapshot.fatigue == 40
    # Non-numeric facts still flow through for prompt/dashboard projection.
    assert snapshot.emotion == "安心"
    assert snapshot.top_events == (event,)
