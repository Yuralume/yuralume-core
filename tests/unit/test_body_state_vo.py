"""Unit tests for :class:`BodyState` VO (HUMANIZATION_ROADMAP §4.1)."""

from __future__ import annotations

import pytest

from kokoro_link.domain.value_objects.body_state import BodyState


def test_default_is_all_low() -> None:
    state = BodyState()
    assert state.hunger == "low"
    assert state.thirst == "low"
    assert state.sleep_debt == "low"
    assert state.seasonal_allergy == "low"
    assert state.is_default


def test_to_prompt_lines_empty_when_default() -> None:
    assert BodyState().to_prompt_lines() == []


def test_to_prompt_lines_renders_only_nondefault_dimensions() -> None:
    state = BodyState(hunger="high", thirst="low", sleep_debt="medium", seasonal_allergy="low")
    lines = state.to_prompt_lines()
    joined = "\n".join(lines)
    assert "肚子很餓" in joined
    assert "睡眠略不足" in joined
    # 口渴/過敏為 low 不應渲染
    assert "口很渴" not in joined
    assert "鼻塞" not in joined


def test_invalid_band_raises() -> None:
    with pytest.raises(ValueError):
        BodyState(hunger="WILTED")


def test_payload_round_trip() -> None:
    original = BodyState(hunger="high", sleep_debt="medium")
    payload = original.to_payload()
    restored = BodyState.from_payload(payload)
    assert restored == original


def test_from_payload_ignores_unknown_keys() -> None:
    restored = BodyState.from_payload({"hunger": "high", "menstrual": "high"})
    assert restored.hunger == "high"
    # ``menstrual`` (owner deliberately excluded) silently dropped, not crashing.
    assert not hasattr(restored, "menstrual")


def test_with_overrides() -> None:
    base = BodyState(hunger="high")
    updated = base.with_overrides(thirst="high")
    assert updated.hunger == "high"
    assert updated.thirst == "high"


def test_empty_string_normalises_to_low() -> None:
    state = BodyState(hunger="", thirst="  ", sleep_debt=None)  # type: ignore[arg-type]
    assert state.is_default
