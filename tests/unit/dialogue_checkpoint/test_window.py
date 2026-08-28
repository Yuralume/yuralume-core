"""The geometry: where covered / middle / raw tail begin and end.

These are the boundaries the reader and the updater both consult, so a
bug here is a message that either appears twice or disappears entirely.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.dialogue_checkpoint.window import (
    fit_to_budget,
    message_tokens,
    split_window,
    total_tokens,
    window_pressure_threshold,
)
from kokoro_link.domain.entities.dialogue_checkpoint import DialogueCheckpoint
from tests.unit.dialogue_checkpoint.builders import (
    CHARACTER_ID,
    NOW,
    OPERATOR_ID,
    assistant_message,
    at,
    conversation_of,
    user_message,
)


def _checkpoint_through(messages, index: int, *, summary: str = "摘要"):
    return DialogueCheckpoint.create(
        character_id=CHARACTER_ID,
        operator_id=OPERATOR_ID,
        summary_text=summary,
        boundary=messages[index],
        now=NOW,
    )


# --- split_window ------------------------------------------------------


def test_no_checkpoint_leaves_everything_uncovered() -> None:
    messages = conversation_of(10)
    window = split_window(messages, checkpoint=None, raw_tail_limit=3)
    assert window.covered == ()
    assert len(window.middle) == 7
    assert len(window.raw_tail) == 3
    assert list(window.uncovered) == messages


def test_a_checkpoint_takes_everything_up_to_its_boundary() -> None:
    messages = conversation_of(10)
    window = split_window(
        messages, checkpoint=_checkpoint_through(messages, 3), raw_tail_limit=3,
    )
    assert list(window.covered) == messages[:4]
    assert list(window.middle) == messages[4:7]
    assert list(window.raw_tail) == messages[7:]


def test_the_boundary_message_itself_is_covered() -> None:
    """Coverage is inclusive of the message the summary ends at — the
    updater builds the checkpoint *from* it."""
    messages = conversation_of(6)
    checkpoint = _checkpoint_through(messages, 2)
    assert checkpoint.covers(messages[2])
    assert not checkpoint.covers(messages[3])


def test_a_checkpoint_with_no_text_covers_nothing() -> None:
    """An empty summary is not a summary. Treating it as coverage would
    hide the messages it claims to have absorbed."""
    messages = conversation_of(8)
    window = split_window(
        messages,
        checkpoint=_checkpoint_through(messages, 4, summary=""),
        raw_tail_limit=3,
    )
    assert window.covered == ()
    assert len(window.uncovered) == 8


def test_a_short_window_is_all_raw_tail() -> None:
    messages = conversation_of(2)
    window = split_window(messages, checkpoint=None, raw_tail_limit=3)
    assert window.middle == ()
    assert list(window.raw_tail) == messages


def test_an_empty_window_is_three_empties() -> None:
    window = split_window([], checkpoint=None, raw_tail_limit=3)
    assert window.covered == window.middle == window.raw_tail == ()


def test_a_zero_tail_limit_is_a_programming_error() -> None:
    """Not clamped silently: a tail of zero would let the checkpoint
    swallow the turn the player can still undo."""
    with pytest.raises(ValueError):
        split_window(conversation_of(4), checkpoint=None, raw_tail_limit=0)


def test_messages_at_the_boundary_instant_are_told_apart_by_content() -> None:
    """Two rows can share a wall-clock stamp. Only the one the summary
    actually ended at is covered; the other stays raw, which is the safe
    direction — showing a message twice is recoverable, losing it is not.
    """
    when = at(100)
    first = user_message("一樣的時間，不一樣的內容", when)
    second = assistant_message("另一則同一秒的訊息", when)
    later = user_message("之後的訊息", at(99))
    checkpoint = DialogueCheckpoint.create(
        character_id=CHARACTER_ID,
        operator_id=OPERATOR_ID,
        summary_text="摘要",
        boundary=first,
        now=NOW,
    )
    assert checkpoint.covers(first)
    assert not checkpoint.covers(second)
    assert not checkpoint.covers(later)


# --- fit_to_budget -----------------------------------------------------


def test_a_generous_budget_keeps_the_whole_middle() -> None:
    messages = conversation_of(10)
    window = split_window(messages, checkpoint=None, raw_tail_limit=3)
    kept = fit_to_budget(
        window.middle, raw_tail=window.raw_tail, budget_tokens=100_000,
    )
    assert kept == window.middle


def test_the_budget_drops_the_oldest_middle_first() -> None:
    messages = conversation_of(10)
    window = split_window(messages, checkpoint=None, raw_tail_limit=3)
    budget = total_tokens(window.raw_tail) + total_tokens(window.middle[-2:])
    kept = fit_to_budget(
        window.middle, raw_tail=window.raw_tail, budget_tokens=budget,
    )
    assert kept == window.middle[-2:]


def test_the_raw_tail_is_never_trimmed_even_when_it_blows_the_budget() -> None:
    """A budget that could eat the tail would let one long reply shorten
    the context of the reply after it."""
    messages = conversation_of(10)
    window = split_window(messages, checkpoint=None, raw_tail_limit=3)
    kept = fit_to_budget(
        window.middle, raw_tail=window.raw_tail, budget_tokens=1,
    )
    assert kept == ()


def test_message_tokens_tracks_content_length() -> None:
    short = user_message("嗯", at(10))
    long = user_message("嗯" * 200, at(9))
    assert message_tokens(long) > message_tokens(short)
    assert total_tokens((short, long)) == (
        message_tokens(short) + message_tokens(long)
    )


# --- the other geometry: how full the window may get ------------------


def test_the_pressure_threshold_leaves_room_for_one_retry() -> None:
    """A merge that fires exactly when the middle band is full is too
    late. The run that fires can decline — an empty merge, a refused
    inflation, a lost CAS — and by the next turn the oldest middle
    message is outside the loaded window: not in the summary, not in the
    prompt, and unreachable by any later run."""
    threshold = window_pressure_threshold(
        window_messages=30, raw_tail_limit=3,
    )
    assert threshold < 30 - 3
    assert threshold >= 1


def test_the_pressure_threshold_tracks_the_window_it_is_given() -> None:
    """It is a fact about the configured window, not a constant. A
    deployment that widens the window gets a later backstop."""
    narrow = window_pressure_threshold(window_messages=30, raw_tail_limit=3)
    wide = window_pressure_threshold(window_messages=60, raw_tail_limit=3)
    assert wide > narrow


def test_a_degenerate_window_still_merges_rather_than_never() -> None:
    """A tail as wide as the window leaves no middle-band capacity at
    all. The answer is "merge as soon as there is anything to merge",
    never a threshold of zero — which would fire on an empty backlog —
    and never a negative one."""
    assert window_pressure_threshold(
        window_messages=4, raw_tail_limit=4,
    ) == 1
    assert window_pressure_threshold(
        window_messages=2, raw_tail_limit=8,
    ) == 1
