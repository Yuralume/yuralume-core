"""Lazy rest-recovery for character state.

When a character has been idle (no chat activity), fatigue should
decrease and energy should increase over time. Instead of running a
scheduler, recovery is computed lazily each time the character is
loaded for a chat turn.

Formula (exponential decay toward resting baseline):

    fatigue_new = fatigue * exp(-elapsed / half_life)
    energy_new  = 100 - (100 - energy) * exp(-elapsed / half_life)

With a 4-hour half-life:
  - 30 min  → ~91% remaining fatigue
  - 1 hour  → ~84%
  - 4 hours → 50%
  - 8 hours → 25%
  - 24 hours → ~1.5%
"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone

from kokoro_link.domain.value_objects.character_state import CharacterState

HALF_LIFE_HOURS = 4.0
_HALF_LIFE_SECONDS = HALF_LIFE_HOURS * 3600.0

# Below this fatigue / energy-deficit we snap to the baseline to avoid
# pointless fractional updates every turn.
_SNAP_THRESHOLD = 1


def apply_rest_recovery(
    state: CharacterState,
    now: datetime | None = None,
) -> CharacterState:
    """Return *state* with fatigue/energy adjusted for idle time.

    If ``last_active_at`` is ``None`` (new character, never chatted),
    no recovery is applied — the initial values are used as-is.
    """
    if state.last_active_at is None:
        return state

    now = now or datetime.now(timezone.utc)
    elapsed = (now - state.last_active_at).total_seconds()
    if elapsed <= 0:
        return state

    decay = math.exp(-elapsed * math.log(2) / _HALF_LIFE_SECONDS)

    new_fatigue = round(state.fatigue * decay)
    if new_fatigue < _SNAP_THRESHOLD:
        new_fatigue = 0

    energy_deficit = 100 - state.energy
    recovered_deficit = round(energy_deficit * decay)
    if recovered_deficit < _SNAP_THRESHOLD:
        recovered_deficit = 0
    new_energy = 100 - recovered_deficit

    if new_fatigue == state.fatigue and new_energy == state.energy:
        return state

    return replace(state, fatigue=new_fatigue, energy=new_energy)
