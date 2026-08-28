"""Reader and updater must be looking at the same list.

A proactive push delivered to both the web thread and a bound LINE
thread exists as two database rows with the same text. The chat prompt
has collapsed those mirrors at its load point since before DH3. The
checkpoint updater loaded the raw rows instead — and that is not a
cosmetic difference, because both sides slice the same list into
covered / middle / raw tail by *position from the end*.

With one mirrored delivery in the last three rows, "the newest three
messages" means two distinct messages to the reader and three rows (two
of them the same sentence) to the updater. The updater then computes a
boundary that the reader considers to be inside its raw tail, and D5 —
"the checkpoint never covers a message the player can still undo" —
stops holding on exactly the accounts that have a channel bound. The
duplicated text is also weighed twice against the trigger and against
the inflation ceiling.

So the fix is not "dedupe in the updater too" but "there is one loader",
and what is pinned here is the consequence, in the shape the bug takes.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest

from kokoro_link.application.services.dialogue_checkpoint import (
    CheckpointUpdateOutcome,
    DialogueCheckpointReader,
    DialogueCheckpointUpdater,
    split_window,
)
from kokoro_link.domain.services.mirrored_message_dedup import (
    dedupe_mirrored_messages,
)
from kokoro_link.infrastructure.repositories.in_memory_dialogue_checkpoints import (
    InMemoryDialogueCheckpointRepository,
)
from tests.unit.dialogue_checkpoint.builders import (
    CHARACTER_ID,
    NOW,
    OPERATOR_ID,
    FakeMerger,
    StubConversationRepository,
    assistant_message,
    at,
    character,
    conversation_of,
)

pytestmark = pytest.mark.asyncio

RAW_TAIL = 3


def mirrored_history(count: int = 14) -> list:
    """A conversation whose last two lines were each fanned out twice.

    Two mirrors, not one, and that is the point rather than
    thoroughness. The raw tail is three *rows*. Each mirrored delivery
    inside it costs one row of real protection, so one mirror merely
    narrows the guard from three messages to two — still enough to cover
    the two-message turn a player can undo, and the bug stays invisible.
    The second mirror is what pushes a message of the *current turn* out
    of the tail and into the band the updater is allowed to absorb.

    A pair with a channel bound reaches this state on any turn where the
    character sends twice, which is not an exotic conversation.
    """
    history = conversation_of(count)
    # An exact copy, role included. A fan-out mirror *is* the same
    # message written to a second thread, and the dedup key covers role,
    # kind, text and attachments — an echo that differs in any of them
    # would not be collapsed, and the fixture would prove nothing.
    for original in list(history[-2:]):
        history.append(replace(original))
    history.sort(key=lambda message: message.created_at)
    return history


def _updater(history, *, checkpoints, merger=None, window=60):
    return DialogueCheckpointUpdater(
        checkpoints=checkpoints,
        merger=merger or FakeMerger(["累積摘要"]),
        conversations=StubConversationRepository(history),
        window_messages=window,
        raw_tail_limit=RAW_TAIL,
        backlog_trigger_tokens=1,
    )


async def test_the_mirrors_really_do_eat_the_tail_the_updater_sees() -> None:
    """The premise, checked before anything depends on it.

    Three rows of raw tail hold only one *distinct* message once two of
    them are mirrors — so a message of the newest turn is sitting in the
    band the updater treats as fair game.
    """
    history = mirrored_history()
    deduped = dedupe_mirrored_messages(history)
    assert len(deduped) == len(history) - 2

    raw_tail = split_window(
        list(history), checkpoint=None, raw_tail_limit=RAW_TAIL,
    ).raw_tail
    assert len({m.content for m in raw_tail}) < RAW_TAIL

    newest_turn = {m.content for m in deduped[-2:]}
    middle = split_window(
        list(history), checkpoint=None, raw_tail_limit=RAW_TAIL,
    ).middle
    assert newest_turn & {m.content for m in middle}


async def test_the_checkpoint_never_absorbs_a_turn_the_player_can_undo() -> None:
    """D5, the invariant the whole rollback design leans on.

    An undo reverses the newest turn. The checkpoint has no un-merge, so
    the one defence is that it never reaches that turn in the first
    place — which holds only if the updater is counting the same
    messages the prompt and the undo path are. Counting mirror copies as
    separate rows makes the three-message guard shorter than three
    messages, and it stops covering the turn it exists to protect.
    """
    history = mirrored_history()
    checkpoints = InMemoryDialogueCheckpointRepository()

    report = await _updater(history, checkpoints=checkpoints).run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )
    assert report.outcome is CheckpointUpdateOutcome.WRITTEN

    checkpoint = await checkpoints.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )
    reversible_turn = dedupe_mirrored_messages(history)[-2:]
    assert not any(checkpoint.covers(m) for m in reversible_turn)


async def test_the_prompt_never_shows_raw_what_the_checkpoint_absorbed() -> None:
    """The same disagreement seen from the reader's side: a message may
    not be both inside the summary and rendered verbatim underneath it."""
    history = mirrored_history()
    checkpoints = InMemoryDialogueCheckpointRepository()
    await _updater(history, checkpoints=checkpoints).run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    reader = DialogueCheckpointReader(
        checkpoints=checkpoints,
        raw_tail_limit=RAW_TAIL,
        prompt_budget_tokens=100_000,
    )
    context = await reader.read(
        character_id=CHARACTER_ID,
        operator_id=OPERATOR_ID,
        recent_messages=dedupe_mirrored_messages(history),
    )
    assert context is not None

    checkpoint = await checkpoints.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )
    assert not any(checkpoint.covers(m) for m in context.messages)
    assert len(context.messages) >= RAW_TAIL


async def test_a_mirrored_line_is_summarised_once_not_twice() -> None:
    """The merge input is what the prompt would have shown, so the model
    is not told the character said the same sentence twice — and the
    token budget is not charged for it twice either."""
    history = conversation_of(14)
    history.append(replace(history[7]))
    history.sort(key=lambda m: m.created_at)
    checkpoints = InMemoryDialogueCheckpointRepository()
    merger = FakeMerger(["累積摘要"])

    await _updater(history, checkpoints=checkpoints, merger=merger).run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    handed = merger.calls[0][1]
    assert len(handed) == len(set(handed))


async def test_both_sides_load_through_the_same_function() -> None:
    """Structural, deliberately.

    A behavioural test can only show that the two agree on the fixtures
    it happens to build. What must hold is that there is one loader —
    the failure mode is a *second* copy of "fetch then dedupe" drifting
    from the first, and only the wiring can rule that out.
    """
    import inspect

    from kokoro_link.application.services import (
        chat_service, dialogue_window_loader,
    )
    from kokoro_link.application.services.dialogue_checkpoint import updater

    assert "load_unified_recent_messages" in inspect.getsource(
        chat_service.ChatService._load_unified_recent_messages,
    )
    assert "load_unified_recent_window" in inspect.getsource(
        updater.DialogueCheckpointUpdater._load_window,
    )
    # Two entry points, one implementation: the prompt's is a wrapper
    # over the updater's, so "fetch then dedupe" happens in exactly one
    # place no matter which door a caller comes through.
    assert (
        chat_service.load_unified_recent_messages
        is dialogue_window_loader.load_unified_recent_messages
    )
    assert (
        updater.load_unified_recent_window
        is dialogue_window_loader.load_unified_recent_window
    )
    assert "load_unified_recent_window" in inspect.getsource(
        dialogue_window_loader.load_unified_recent_messages,
    )


async def test_neither_side_can_nominate_which_mirror_copy_survives() -> None:
    """One loader is not enough if the loader takes a per-caller knob.

    The prompt used to pass ``preferred=conversation.messages`` so a
    rendered transcript kept its own thread's row; the background
    updater has no conversation and passed nothing, so it kept the
    earliest. Same sentence, two different ``created_at`` values — and
    the coverage cursor *is* a timestamp plus a fingerprint of the
    surviving row. The two sides then answered ``covers()`` differently
    about the boundary message itself, which is the one row where
    disagreeing puts the same line inside the summary and verbatim in
    the transcript underneath it.

    Structural, because behaviour cannot express it any more: the knob
    is gone, so nothing can pass one.
    """
    import inspect

    from kokoro_link.application.services import dialogue_window_loader
    from kokoro_link.domain.services import mirrored_message_dedup

    for function in (
        dialogue_window_loader.load_unified_recent_window,
        dialogue_window_loader.load_unified_recent_messages,
        mirrored_message_dedup.dedupe_mirrored_messages,
    ):
        assert "preferred" not in inspect.signature(function).parameters


async def test_the_boundary_row_survives_as_the_same_copy_on_both_sides(
) -> None:
    """The consequence, on the row where it bites.

    A mirrored delivery sitting exactly on the coverage boundary: the
    updater writes a cursor naming one copy, and the reader has to agree
    that the copy it holds is that one. Disagree and the boundary
    message is rendered raw *and* summarised.
    """
    history = conversation_of(14)
    # The echo arrives 40 seconds later, on the bound channel, and would
    # have been the survivor for a caller that nominated its thread.
    echo = replace(history[10], created_at=history[10].created_at + timedelta(
        seconds=40,
    ))
    history.append(echo)
    history.sort(key=lambda message: message.created_at)
    checkpoints = InMemoryDialogueCheckpointRepository()

    await _updater(history, checkpoints=checkpoints).run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )
    checkpoint = await checkpoints.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )

    rendered = dedupe_mirrored_messages(history)
    assert echo not in rendered
    covered = [m for m in rendered if checkpoint.covers(m)]
    assert covered
    assert not set(covered) & set(rendered[-RAW_TAIL:])


async def test_the_updater_asks_for_the_window_it_was_configured_with() -> None:
    history = conversation_of(40)
    conversations = StubConversationRepository(history)
    machine = DialogueCheckpointUpdater(
        checkpoints=InMemoryDialogueCheckpointRepository(),
        merger=FakeMerger(["累積摘要"]),
        conversations=conversations,
        window_messages=25,
        raw_tail_limit=RAW_TAIL,
        backlog_trigger_tokens=1,
    )

    await machine.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert conversations.requested_limits
    assert set(conversations.requested_limits) == {25}


async def test_at_least_one_mirror_fixture_is_not_silently_a_no_op() -> None:
    """Guards the suite against itself: if ``dedupe_mirrored_messages``
    ever stops collapsing this fixture — a window change, a key change —
    every test above would keep passing while testing nothing."""
    history = mirrored_history()
    assert len(dedupe_mirrored_messages(history)) < len(history)
    assert at(0) is not None
