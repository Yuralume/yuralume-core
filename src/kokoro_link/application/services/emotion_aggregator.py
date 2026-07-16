"""Exponential-decay aggregator over EmotionEvent streams.

Pure function — no IO, deterministic given inputs. Folding logic:

1. For each event compute ``weight = 2 ** (-elapsed_min / half_life_min)``.
   Events past ``expires_at`` count as weight 0.
2. Integer deltas (``affection_delta`` etc.) multiplied by weight and
   added to the corresponding baseline; clamped to [0, 100].
3. Continuous valence / arousal computed as intensity-weighted mean
   over weight-decayed events.
4. ``emotion`` string = label from the most recent ``cause_ref_kind=turn``
   event (per the project's LLM-first contract: the LLM names emotions;
   we just route the most recent name through).
5. ``top_events`` = events sorted by ``intensity * weight`` desc, take
   first ``top_k``.

Why no behavioural branching here — per ``CLAUDE.md`` §LLM-first the
rule is "facts only; the LLM decides what to do with them". This module
emits computed facts; how prompts react to them is the LLM's job.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from kokoro_link.contracts.emotion import EmotionAggregatorPort, EmotionSnapshot
from kokoro_link.domain.entities.emotion_event import (
    CAUSE_TURN,
    EmotionEvent,
)


def _clamp_int(value: float) -> int:
    if value <= 0:
        return 0
    if value >= 100:
        return 100
    return int(round(value))


def _clamp_signed(value: float) -> float:
    return max(-1.0, min(1.0, value))


def _clamp_unit(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass(frozen=True, slots=True)
class _WeightedEvent:
    event: EmotionEvent
    weight: float


class ExponentialDecayEmotionAggregator(EmotionAggregatorPort):
    """Reference implementation.

    Stateless — safe to share across requests.
    """

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
        weighted = [_weight_event(e, now) for e in events]
        # Keep only non-trivially-weighted ones — events whose weight has
        # decayed below 0.01 (~7 half-lives) add nothing meaningful and
        # only inflate top_events.
        alive = [w for w in weighted if w.weight > 0.01]

        aff = baseline_affection
        fat = baseline_fatigue
        tru = baseline_trust
        eng = baseline_energy
        valence_num = 0.0
        arousal_num = 0.0
        intensity_total = 0.0
        for we in alive:
            if not we.event.applied_to_state:
                aff += we.event.affection_delta * we.weight
                fat += we.event.fatigue_delta * we.weight
                tru += we.event.trust_delta * we.weight
                eng += we.event.energy_delta * we.weight
            i = we.event.intensity * we.weight
            valence_num += we.event.valence * i
            arousal_num += we.event.arousal * i
            intensity_total += i

        if intensity_total > 0:
            valence = _clamp_signed(valence_num / intensity_total)
            arousal = _clamp_unit(arousal_num / intensity_total)
        else:
            valence = 0.0
            arousal = 0.0

        emotion = _latest_turn_label(events) or baseline_emotion

        ranked = sorted(
            alive,
            key=lambda we: we.event.intensity * we.weight,
            reverse=True,
        )
        top_events = tuple(we.event for we in ranked[:top_k])

        return EmotionSnapshot(
            emotion=emotion,
            affection=_clamp_int(aff),
            fatigue=_clamp_int(fat),
            trust=_clamp_int(tru),
            energy=_clamp_int(eng),
            valence=valence,
            arousal=arousal,
            top_events=top_events,
        )


def _weight_event(event: EmotionEvent, now: datetime) -> _WeightedEvent:
    if event.expires_at is not None and event.expires_at <= now:
        return _WeightedEvent(event=event, weight=0.0)
    elapsed_minutes = max(0.0, (now - event.created_at).total_seconds() / 60.0)
    half_life = max(1, event.decay_half_life_minutes)
    weight = math.pow(2.0, -elapsed_minutes / half_life)
    return _WeightedEvent(event=event, weight=weight)


def _latest_turn_label(events: list[EmotionEvent]) -> str:
    latest: EmotionEvent | None = None
    for e in events:
        if e.cause_ref_kind != CAUSE_TURN:
            continue
        if not e.emotion_label.strip():
            continue
        if latest is None or e.created_at > latest.created_at:
            latest = e
    return latest.emotion_label if latest is not None else ""
