"""Unit tests for lazy rest-recovery."""

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.state.recovery import (
    HALF_LIFE_HOURS,
    apply_rest_recovery,
)


def _state(
    fatigue: int = 80,
    energy: int = 20,
    last_active_at: datetime | None = None,
) -> CharacterState:
    return CharacterState(
        emotion="neutral",
        affection=50,
        fatigue=fatigue,
        trust=50,
        energy=energy,
        last_active_at=last_active_at,
    )


class TestApplyRestRecovery:
    def test_no_last_active_returns_unchanged(self) -> None:
        state = _state(last_active_at=None)
        result = apply_rest_recovery(state)
        assert result is state

    def test_zero_elapsed_returns_unchanged(self) -> None:
        now = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
        state = _state(last_active_at=now)
        result = apply_rest_recovery(state, now=now)
        assert result.fatigue == 80
        assert result.energy == 20

    def test_one_half_life_halves_fatigue(self) -> None:
        base = datetime(2026, 4, 17, 8, 0, 0, tzinfo=timezone.utc)
        now = base + timedelta(hours=HALF_LIFE_HOURS)
        state = _state(fatigue=80, energy=20, last_active_at=base)
        result = apply_rest_recovery(state, now=now)
        assert result.fatigue == 40  # 80 * 0.5 = 40
        assert result.energy == 60  # 100 - (100-20)*0.5 = 100-40 = 60

    def test_two_half_lives_quarters_fatigue(self) -> None:
        base = datetime(2026, 4, 17, 8, 0, 0, tzinfo=timezone.utc)
        now = base + timedelta(hours=HALF_LIFE_HOURS * 2)
        state = _state(fatigue=80, energy=20, last_active_at=base)
        result = apply_rest_recovery(state, now=now)
        assert result.fatigue == 20  # 80 * 0.25 = 20
        assert result.energy == 80  # 100 - 80*0.25 = 80

    def test_24_hours_near_full_recovery(self) -> None:
        base = datetime(2026, 4, 17, 0, 0, 0, tzinfo=timezone.utc)
        now = base + timedelta(hours=24)
        state = _state(fatigue=100, energy=0, last_active_at=base)
        result = apply_rest_recovery(state, now=now)
        assert result.fatigue <= 2  # nearly zero
        assert result.energy >= 98  # nearly full

    def test_short_idle_minor_recovery(self) -> None:
        base = datetime(2026, 4, 17, 12, 0, 0, tzinfo=timezone.utc)
        now = base + timedelta(minutes=30)
        state = _state(fatigue=80, energy=20, last_active_at=base)
        result = apply_rest_recovery(state, now=now)
        # 30 min with 4h half-life: decay ≈ 0.917
        assert 70 <= result.fatigue <= 75
        assert 25 <= result.energy <= 30

    def test_already_rested_no_change(self) -> None:
        base = datetime(2026, 4, 17, 8, 0, 0, tzinfo=timezone.utc)
        now = base + timedelta(hours=1)
        state = _state(fatigue=0, energy=100, last_active_at=base)
        result = apply_rest_recovery(state, now=now)
        assert result.fatigue == 0
        assert result.energy == 100

    def test_negative_elapsed_returns_unchanged(self) -> None:
        """Guard against clock skew — future last_active_at."""
        now = datetime(2026, 4, 17, 8, 0, 0, tzinfo=timezone.utc)
        future = now + timedelta(hours=1)
        state = _state(fatigue=80, energy=20, last_active_at=future)
        result = apply_rest_recovery(state, now=now)
        assert result.fatigue == 80
        assert result.energy == 20

    def test_snap_threshold_avoids_fractional_residue(self) -> None:
        """Very long idle should snap to 0 fatigue / 100 energy, not leave residual."""
        base = datetime(2026, 4, 17, 0, 0, 0, tzinfo=timezone.utc)
        now = base + timedelta(days=7)
        state = _state(fatigue=50, energy=50, last_active_at=base)
        result = apply_rest_recovery(state, now=now)
        assert result.fatigue == 0
        assert result.energy == 100
