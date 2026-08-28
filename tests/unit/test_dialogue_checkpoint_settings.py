"""``DialogueCheckpointSettings.from_env`` — and the default that matters.

The flag being **off** is not a preference, it is DH3's D8 contract: the
read path is a deliberate behaviour change (last-good on failure instead
of falling back to the full raw message list) and wants real
conversations behind a flag before it becomes anyone's default. A test
that only checked "the parser works" would let a stray default flip go
out unremarked, so the off-by-default is asserted first and on its own.
"""

from __future__ import annotations

import logging

import pytest

from kokoro_link.bootstrap.settings import (
    MINIMUM_DIALOGUE_WINDOW_MESSAGES,
    DialogueCheckpointSettings,
)

_ENV_VARS = (
    "KOKORO_DIALOGUE_CHECKPOINT_ENABLED",
    "KOKORO_DIALOGUE_CHECKPOINT_WINDOW_MESSAGES",
    "KOKORO_DIALOGUE_CHECKPOINT_PROMPT_BUDGET_TOKENS",
    "KOKORO_DIALOGUE_CHECKPOINT_BACKLOG_TRIGGER_TOKENS",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def test_the_feature_is_off_unless_a_deployment_says_otherwise(
    clean_env: None,
) -> None:
    assert DialogueCheckpointSettings.from_env().enabled is False
    assert DialogueCheckpointSettings().enabled is False


def test_the_defaults_are_the_documented_ones(clean_env: None) -> None:
    settings = DialogueCheckpointSettings.from_env()
    assert settings.window_messages == 30
    assert settings.prompt_budget_tokens == 2400
    assert settings.backlog_trigger_tokens == 400


def test_the_default_trigger_is_reachable_at_the_default_window() -> None:
    """The bug this pins is invisible in every other test in the suite.

    A trigger denominated in tokens, applied to a backlog whose size is
    capped in *messages*, can be set past what the cap allows — and then
    the whole feature does nothing while looking perfectly configured.
    The shipped default was 1500 against a middle band that tops out
    around 1100 on generous assumptions and 500 on realistic ones.

    So the defaults are checked against each other rather than each on
    its own: the trigger must sit below what a full middle band of
    ordinary Traditional-Chinese chat can weigh, at the low end of the
    per-message range, or it is unsatisfiable in practice.
    """
    settings = DialogueCheckpointSettings()
    raw_tail = 3
    middle_capacity = settings.window_messages - raw_tail
    lean_tokens_per_message = 20
    assert settings.backlog_trigger_tokens < (
        middle_capacity * lean_tokens_per_message
    )


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_the_flag_accepts_the_usual_truthy_spellings(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, raw: str,
) -> None:
    monkeypatch.setenv("KOKORO_DIALOGUE_CHECKPOINT_ENABLED", raw)
    assert DialogueCheckpointSettings.from_env().enabled is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_the_flag_accepts_the_usual_falsy_spellings(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, raw: str,
) -> None:
    monkeypatch.setenv("KOKORO_DIALOGUE_CHECKPOINT_ENABLED", raw)
    assert DialogueCheckpointSettings.from_env().enabled is False


def test_an_unparseable_flag_keeps_the_safe_default(
    monkeypatch: pytest.MonkeyPatch, clean_env: None,
) -> None:
    """A typo must not turn the feature on. ``_parse_bool`` falls back
    to the default, and the default is the conservative direction."""
    monkeypatch.setenv("KOKORO_DIALOGUE_CHECKPOINT_ENABLED", "maybe")
    assert DialogueCheckpointSettings.from_env().enabled is False


def test_the_numeric_knobs_are_read_from_env(
    monkeypatch: pytest.MonkeyPatch, clean_env: None,
) -> None:
    monkeypatch.setenv("KOKORO_DIALOGUE_CHECKPOINT_WINDOW_MESSAGES", "50")
    monkeypatch.setenv(
        "KOKORO_DIALOGUE_CHECKPOINT_PROMPT_BUDGET_TOKENS", "4000",
    )
    monkeypatch.setenv(
        "KOKORO_DIALOGUE_CHECKPOINT_BACKLOG_TRIGGER_TOKENS", "800",
    )
    settings = DialogueCheckpointSettings.from_env()
    assert settings.window_messages == 50
    assert settings.prompt_budget_tokens == 4000
    assert settings.backlog_trigger_tokens == 800


@pytest.mark.parametrize("raw", ["0", "-5", "not-a-number"])
def test_a_nonsense_number_cannot_produce_a_zero_window(
    monkeypatch: pytest.MonkeyPatch, clean_env: None, raw: str,
) -> None:
    """Clamped rather than trusted: a window of zero would load no
    messages at all, and a trigger of zero would fire a merge on every
    single turn — reinstating the exact cost DH3 removes."""
    for name in _ENV_VARS[1:]:
        monkeypatch.setenv(name, raw)
    settings = DialogueCheckpointSettings.from_env()
    assert settings.window_messages >= 1
    assert settings.prompt_budget_tokens >= 1
    assert settings.backlog_trigger_tokens >= 1


# --- the window has a floor, not just a positivity check ---------------


@pytest.mark.parametrize("raw", ["1", "3", "5", "7"])
def test_a_window_below_the_floor_is_raised_to_it(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    clean_env: None,
    raw: str,
) -> None:
    """``>= 1`` is not a validation, it is a type check.

    Every value it let through below the floor is broken, and each is
    broken silently. At one or three the raw tail eats the whole window:
    the middle band is empty on every turn, no checkpoint is ever
    written, and the feature looks perfectly configured. At five or
    seven the pressure backstop's margin is already spent, so a merge
    fires on essentially every turn — the per-turn LLM call this feature
    exists to remove. And all of them starve the "no checkpoint yet"
    fallback, which slices the last eight messages off a list that now
    holds fewer. The clamp is the only one of these that says anything
    out loud.
    """
    monkeypatch.setenv("KOKORO_DIALOGUE_CHECKPOINT_WINDOW_MESSAGES", raw)
    with caplog.at_level(logging.WARNING):
        settings = DialogueCheckpointSettings.from_env()

    assert settings.window_messages == MINIMUM_DIALOGUE_WINDOW_MESSAGES
    assert any("floor" in record.message for record in caplog.records)


def test_the_floor_applies_to_direct_construction_too() -> None:
    """The env parser is not the only way in — the embedded container and
    every test construct this dataclass directly."""
    assert DialogueCheckpointSettings(
        window_messages=2,
    ).window_messages == MINIMUM_DIALOGUE_WINDOW_MESSAGES


def test_a_window_at_or_above_the_floor_is_left_alone(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        settings = DialogueCheckpointSettings(
            window_messages=MINIMUM_DIALOGUE_WINDOW_MESSAGES,
        )
    assert settings.window_messages == MINIMUM_DIALOGUE_WINDOW_MESSAGES
    assert not caplog.records


def test_the_floor_is_pinned_to_the_constants_it_is_derived_from() -> None:
    """The floor is only correct *relative* to three numbers in three
    other layers, and nothing in the type system says so.

    It is the pre-DH3 window (turning the feature on may widen what the
    prompt loads, never narrow it), and it has to leave at least one
    middle-band row above the raw tail plus the pressure margin, or the
    geometry that decides when to merge has nothing to work with.
    """
    from kokoro_link.application.services.chat_service import (
        _RECENT_MESSAGE_LIMIT,
    )
    from kokoro_link.application.services.dialogue_checkpoint.window import (
        PROMPT_RAW_TAIL_MESSAGES,
        WINDOW_PRESSURE_SAFETY_MARGIN,
    )

    assert MINIMUM_DIALOGUE_WINDOW_MESSAGES == _RECENT_MESSAGE_LIMIT
    assert MINIMUM_DIALOGUE_WINDOW_MESSAGES > (
        PROMPT_RAW_TAIL_MESSAGES + WINDOW_PRESSURE_SAFETY_MARGIN
    )
    assert DialogueCheckpointSettings().window_messages >= (
        MINIMUM_DIALOGUE_WINDOW_MESSAGES
    )
