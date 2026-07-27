"""Unit tests for the synthetic-soak seeder's deterministic plan (pure)."""

from __future__ import annotations

from pydantic import EmailStr, TypeAdapter

from scripts.soak_seed_population import (
    build_seed_plan,
    operator_email,
    soak_password,
)


def _plan(**overrides):
    kwargs = dict(
        operators=4,
        characters_per=15,
        freeze_fraction=0.15,
        proactive_disabled_fraction=0.2,
        seed=20260720,
    )
    kwargs.update(overrides)
    return build_seed_plan(**kwargs)


def test_plan_is_deterministic_under_seed() -> None:
    a = _plan()
    b = _plan()
    assert a == b  # frozen dataclasses compare structurally
    # A different seed changes the freeze/disable draw.
    c = _plan(seed=1)
    assert c.characters != a.characters


def test_plan_counts_and_fractions() -> None:
    plan = _plan(operators=4, characters_per=15)
    assert len(plan.operators) == 4
    assert len(plan.characters) == 60
    # round(60 * 0.15) = 9, round(60 * 0.2) = 12
    assert plan.frozen_count == 9
    assert plan.proactive_disabled_count == 12


def test_timezone_round_robin_spread() -> None:
    plan = _plan(operators=6, characters_per=1)
    dist = plan.timezone_distribution()
    # Six operators over the six default timezones → one each.
    assert set(dist.values()) == {1}
    assert "Asia/Taipei" in dist
    assert "Pacific/Auckland" in dist


def test_custom_timezones_and_operator_keys() -> None:
    plan = _plan(operators=3, characters_per=2, timezones=("UTC", "Asia/Tokyo"))
    assert [op.timezone_id for op in plan.operators] == [
        "UTC", "Asia/Tokyo", "UTC",
    ]
    assert plan.operators[0].key == "soak-op0"
    assert plan.characters[0].operator_key == "soak-op0"
    assert plan.characters[0].name == "soak-op0-char0"


def test_fraction_bounds_rejected() -> None:
    import pytest

    with pytest.raises(ValueError):
        _plan(freeze_fraction=1.5)
    with pytest.raises(ValueError):
        _plan(proactive_disabled_fraction=-0.1)


def test_zero_fractions_produce_no_frozen_or_disabled() -> None:
    plan = _plan(freeze_fraction=0.0, proactive_disabled_fraction=0.0)
    assert plan.frozen_count == 0
    assert plan.proactive_disabled_count == 0
    assert all(c.proactive_enabled for c in plan.characters)
    assert all(not c.freeze for c in plan.characters)


def test_credentials_deterministic_and_prefixed() -> None:
    email = operator_email("soak", 2)
    assert email == "soak-op2@example.com"
    # The interaction driver submits this value to LoginRequest.email
    # (EmailStr), so the reserved test address must pass that exact validator.
    assert str(TypeAdapter(EmailStr).validate_python(email)) == email
    assert soak_password("soak", 1) == soak_password("soak", 1)
    assert soak_password("soak", 1) != soak_password("soak", 2)
    assert soak_password("soak", 1) != soak_password("other", 1)
    # Never empty; carries the soak marker so it is obviously not a real secret.
    assert soak_password("soak", 1).startswith("Soak-")
