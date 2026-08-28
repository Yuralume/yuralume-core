"""The trigger has to be reachable, and the boundary has to keep up.

Two failures live here, and they are the same failure seen from two
sides. The backlog is capped in *messages* — the middle band can never
exceed ``window - raw_tail`` rows — while the trigger is denominated in
*tokens*. Set the trigger above what that many rows can weigh and it is
not strict, it is unsatisfiable: the merge never fires, no checkpoint is
ever written, and the feature reports nothing wrong because declining to
merge is its most ordinary outcome.

The other side of it is what happens when the merge stalls for any
reason at all — a trigger set too high, or a run of refused merges. The
coverage boundary freezes while the loaded window keeps sliding forward,
and the messages between the two are in neither the summary nor the
prompt. The hole only grows, and nothing ever announces it.

So the geometry is pinned in messages, not in tokens: a merge must fire
before the middle band can fill the window, whatever the trigger says.
"""

from __future__ import annotations

import logging
from dataclasses import replace

import pytest

from kokoro_link.application.services.dialogue_checkpoint import (
    STUCK_STREAK_WARN_AT,
    CheckpointUpdateOutcome,
)
from kokoro_link.bootstrap.settings import DialogueCheckpointSettings
from kokoro_link.llm_output.tokens import estimate_tokens
from tests.unit.dialogue_checkpoint.builders import (
    CHARACTER_ID,
    NOW,
    OPERATOR_ID,
    FailingMerger,
    FakeMerger,
    assistant_message,
    at,
    character,
    conversation_of,
    user_message,
)
from tests.unit.dialogue_checkpoint.test_updater import build_updater, stored

pytestmark = pytest.mark.asyncio

WINDOW = 30
RAW_TAIL = 3
SHIPPED_TRIGGER = 1500


def short_chat(count: int) -> list:
    """The conversation the shipped trigger could not summarise.

    Short Traditional-Chinese lines — the way people actually type in a
    chat window, not the paragraph-length turns the other suites build.
    Each of these estimates at roughly twenty tokens, which is the whole
    point: a full middle band of them still does not weigh 1500.
    """
    lines = ("今天好累喔", "怎麼了嗎", "工作卡住了", "要不要休息一下")
    messages = []
    for index in range(count):
        text = lines[index % len(lines)] + f"（{index}）"
        when = at(600 - index)
        messages.append(
            user_message(text, when) if index % 2 == 0
            else assistant_message(text, when)
        )
    return messages


# --- the trigger the defaults could never reach -------------------------


async def test_a_full_middle_band_of_short_chat_never_weighs_the_old_trigger() -> None:
    """The arithmetic behind the bug, stated once so the tests below
    are not just asserting an implementation back to itself."""
    full_band = short_chat(WINDOW)[: WINDOW - RAW_TAIL]
    assert len(full_band) == WINDOW - RAW_TAIL
    weight = sum(estimate_tokens(m.content) for m in full_band)
    assert weight < SHIPPED_TRIGGER


async def test_short_chat_still_gets_a_checkpoint_under_an_unreachable_trigger() -> None:
    """The regression itself: at the shipped window, a trigger the
    backlog cannot physically reach must not mean "never merge"."""
    updater, repository = build_updater(
        short_chat(WINDOW),
        merger=FakeMerger(["累積摘要"]),
        trigger=SHIPPED_TRIGGER,
        window=WINDOW,
        tail=RAW_TAIL,
    )

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.WRITTEN
    assert report.window_pressure is True
    assert report.backlog_tokens < SHIPPED_TRIGGER
    assert await stored(repository) is not None


async def test_the_boundary_keeps_chasing_the_sliding_window() -> None:
    """Play enough turns that the window slides several times over, and
    check the thing that actually matters: every message is either
    inside the coverage or still visible in the window. A message that
    is in neither has been lost — never summarised, no longer shown."""
    history: list = []
    merger = FakeMerger(["累積摘要"])
    updater, repository = build_updater(
        history, merger=merger, trigger=SHIPPED_TRIGGER,
        window=WINDOW, tail=RAW_TAIL,
    )
    all_messages = short_chat(WINDOW * 4)

    for message in all_messages:
        history.append(message)
        updater._conversations.messages = list(history)
        await updater.run(
            character=character(), operator_id=OPERATOR_ID, now=NOW,
        )

    checkpoint = await stored(repository)
    assert checkpoint is not None
    visible = set(id(m) for m in history[-WINDOW:])
    orphans = [
        m for m in history
        if id(m) not in visible and not checkpoint.covers(m)
    ]
    assert not orphans, (
        f"{len(orphans)} message(s) fell out of the window without ever "
        "having been summarised"
    )


def mirrored_short_chat(count: int, *, mirrors: int) -> list:
    """``short_chat`` with ``mirrors`` of its newest lines fanned out.

    An exact second row for the same delivery — the shape a proactive
    push takes on a pair with LINE or Telegram bound. The loader
    collapses them and, by design, does *not* top the window back up, so
    a 30-row fetch yields fewer than 30 usable messages.
    """
    history = short_chat(count)
    for original in list(history[-(mirrors + 4):-4]):
        history.append(replace(original))
    history.sort(key=lambda message: message.created_at)
    return history


async def test_window_pressure_is_measured_against_the_rows_actually_loaded(
) -> None:
    """The backstop has to survive mirror collapse, or it protects
    exactly the pairs that do not need it.

    The threshold used to be computed from the configured row limit (30
    → 24) while the thing compared against it was the middle band of the
    *deduplicated* list. Six mirrors inside the window leave 24 usable
    rows, so the middle band tops out at 21 and the condition
    ``21 >= 24`` is unreachable — on every pair with a messaging channel
    bound, the geometric backstop was off. Those are the pairs whose
    windows fill fastest.
    """
    history = mirrored_short_chat(WINDOW + 10, mirrors=6)
    updater, repository = build_updater(
        history,
        merger=FakeMerger(["累積摘要"]),
        trigger=SHIPPED_TRIGGER,
        window=WINDOW,
        tail=RAW_TAIL,
    )
    # The premise: a saturated fetch that collapses well below the limit.
    loaded = await updater._load_window(CHARACTER_ID)
    assert loaded.fetched == WINDOW and loaded.saturated
    assert len(loaded.messages) == WINDOW - 6
    assert len(loaded.messages) - RAW_TAIL < WINDOW - RAW_TAIL - 3

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.WRITTEN
    assert report.window_pressure is True
    assert await stored(repository) is not None


async def test_a_history_shorter_than_the_window_is_not_under_pressure(
) -> None:
    """"Fewer rows than I asked for" means two different things.

    Mirrors collapsed (rows are still falling off the back — pressure)
    versus that is the whole history (nothing is falling off anything).
    Measuring the threshold against the loaded length without asking
    which one it is would fire a merge on the twelfth message of a brand
    new pair, on a window that is nowhere near full.
    """
    updater, repository = build_updater(
        short_chat(12),
        merger=FakeMerger(["累積摘要"]),
        trigger=SHIPPED_TRIGGER,
        window=WINDOW,
        tail=RAW_TAIL,
    )

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.BACKLOG_TOO_SMALL
    assert report.backlog_messages == 12 - RAW_TAIL
    assert await stored(repository) is None


async def test_a_well_tuned_trigger_still_fires_on_weight_not_pressure() -> None:
    """The backstop must not quietly become the mechanism.

    With the trigger at the shipped default and turns of ordinary
    paragraph length, the merge happens because the backlog earned it —
    well before the window fills. If this ever starts reporting
    ``window_pressure``, the default has drifted back above what a
    window can hold and the backstop is carrying the feature again.
    """
    updater, _ = build_updater(
        conversation_of(20),
        merger=FakeMerger(["累積摘要"]),
        trigger=DialogueCheckpointSettings().backlog_trigger_tokens,
        window=WINDOW,
        tail=RAW_TAIL,
    )

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.WRITTEN
    assert report.window_pressure is False


# --- a stalled checkpoint has to be audible -----------------------------


async def test_repeated_failures_are_counted_and_eventually_warned_about(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A single failed merge is ordinary. A run of them means the
    boundary is frozen against a moving window, and that condition has
    no other symptom — the player sees nothing, the operator sees
    nothing, and the hole only grows."""
    updater, _ = build_updater(
        short_chat(WINDOW), merger=FailingMerger(),
        trigger=1, window=WINDOW, tail=RAW_TAIL,
    )
    who = character()

    streaks = []
    with caplog.at_level(logging.WARNING):
        for _ in range(STUCK_STREAK_WARN_AT):
            report = await updater.run(
                character=who, operator_id=OPERATOR_ID, now=NOW,
            )
            assert report.outcome is CheckpointUpdateOutcome.MERGE_EMPTY
            streaks.append(report.stuck_streak)

    assert streaks == list(range(1, STUCK_STREAK_WARN_AT + 1))
    assert any(
        "consecutive" in record.message for record in caplog.records
    )


async def test_a_successful_merge_clears_the_streak() -> None:
    """Otherwise a pair that recovers carries an old streak forward and
    crosses the threshold on its first later stumble."""
    history = short_chat(WINDOW)
    updater, _ = build_updater(
        history, merger=FailingMerger(),
        trigger=1, window=WINDOW, tail=RAW_TAIL,
    )
    who = character()
    await updater.run(character=who, operator_id=OPERATOR_ID, now=NOW)
    assert (await updater.run(
        character=who, operator_id=OPERATOR_ID, now=NOW,
    )).stuck_streak == 2

    updater._merger = FakeMerger(["終於壓縮成功"])
    good = await updater.run(
        character=who, operator_id=OPERATOR_ID, now=NOW,
    )
    assert good.outcome is CheckpointUpdateOutcome.WRITTEN
    assert good.stuck_streak == 0

    # A fresh backlog, so the next run genuinely attempts a merge rather
    # than declining for want of material — otherwise "the streak reset"
    # would be indistinguishable from "nothing was tried".
    updater._conversations.messages = short_chat(WINDOW * 2)
    updater._merger = FailingMerger()
    again = await updater.run(
        character=who, operator_id=OPERATOR_ID, now=NOW,
    )
    assert again.outcome is CheckpointUpdateOutcome.MERGE_EMPTY
    assert again.stuck_streak == 1


async def test_one_pair_s_streak_is_not_another_pair_s() -> None:
    other = character(character_id="char-2")
    updater, _ = build_updater(
        short_chat(WINDOW), merger=FailingMerger(),
        trigger=1, window=WINDOW, tail=RAW_TAIL,
    )
    await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )
    await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )
    report = await updater.run(
        character=other, operator_id=OPERATOR_ID, now=NOW,
    )
    assert report.stuck_streak == 1
    assert CHARACTER_ID != other.id
