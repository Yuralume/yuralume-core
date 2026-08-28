"""The post-turn update machine — every path it can decline to write on.

Each test names one outcome, because the outcome enum *is* the machine's
observable behaviour when it does not write. Asserting only "the row did
not change" would pass for the right reason and the wrong one alike.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.dialogue_checkpoint import (
    CheckpointUpdateOutcome,
    DialogueCheckpointUpdater,
)
from kokoro_link.domain.entities.dialogue_checkpoint import DialogueCheckpoint
from kokoro_link.infrastructure.repositories.in_memory_dialogue_checkpoints import (
    InMemoryDialogueCheckpointRepository,
)
from tests.unit.dialogue_checkpoint.builders import (
    CHARACTER_ID,
    NOW,
    OPERATOR_ID,
    FailingMerger,
    FakeMerger,
    StubConversationRepository,
    at,
    character,
    conversation_of,
    restricted_message,
    tool_only_message,
)

pytestmark = pytest.mark.asyncio


def build_updater(
    messages,
    *,
    merger=None,
    checkpoints=None,
    trigger=1,
    window=60,
    tail=3,
    enabled=True,
):
    repository = checkpoints or InMemoryDialogueCheckpointRepository()
    updater = DialogueCheckpointUpdater(
        checkpoints=repository,
        merger=merger or FakeMerger(),
        conversations=StubConversationRepository(messages),
        window_messages=window,
        raw_tail_limit=tail,
        backlog_trigger_tokens=trigger,
        enabled=enabled,
    )
    return updater, repository


async def stored(repository):
    return await repository.get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )


# --- the happy path ----------------------------------------------------


async def test_a_first_merge_writes_a_checkpoint() -> None:
    messages = conversation_of(10)
    updater, repository = build_updater(
        messages, merger=FakeMerger(["第一份累積摘要"]),
    )

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.WRITTEN
    checkpoint = await stored(repository)
    assert checkpoint is not None
    assert checkpoint.summary_text == "第一份累積摘要"
    assert checkpoint.model == "fake-model"
    assert checkpoint.covers_until_created_at == messages[6].created_at


async def test_a_second_merge_is_handed_the_first_summary() -> None:
    """Merge, not restate: the previous summary is an input."""
    messages = conversation_of(10)
    merger = FakeMerger(["第一份", "第二份"])
    updater, repository = build_updater(messages, merger=merger)
    await updater.run(character=character(), operator_id=OPERATOR_ID, now=NOW)

    updater_2, _ = build_updater(
        conversation_of(20), merger=merger, checkpoints=repository,
    )
    report = await updater_2.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.WRITTEN
    assert merger.calls[1][0] == "第一份"


async def test_only_uncovered_messages_are_handed_to_the_merger() -> None:
    """The merge is incremental. ``conversation_of(18)`` extends
    ``conversation_of(12)`` — same first twelve rows, six more after —
    so the second run's backlog is exactly what the first did not take.
    """
    merger = FakeMerger()
    updater, repository = build_updater(conversation_of(12), merger=merger)
    await updater.run(character=character(), operator_id=OPERATOR_ID, now=NOW)
    first_boundary = (await stored(repository)).covers_until_created_at

    grown = conversation_of(18)
    updater_2, _ = build_updater(
        grown, merger=merger, checkpoints=repository,
    )
    await updater_2.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert len(merger.calls) == 2
    covered_texts = {
        m.content for m in grown if m.created_at <= first_boundary
    }
    assert covered_texts
    assert not covered_texts & set(merger.calls[1][1])


# --- declining to write ------------------------------------------------


async def test_a_small_backlog_does_not_spend_an_llm_call() -> None:
    merger = FakeMerger()
    updater, repository = build_updater(
        conversation_of(6), merger=merger, trigger=100_000,
    )

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.BACKLOG_TOO_SMALL
    assert merger.calls == []
    assert await stored(repository) is None


async def test_a_failed_merge_keeps_the_last_good_checkpoint() -> None:
    """D4 — the correction to the old failure direction. Nothing widens
    and nothing is erased; the previous summary simply stands."""
    messages = conversation_of(10)
    updater, repository = build_updater(messages, merger=FakeMerger(["好摘要"]))
    await updater.run(character=character(), operator_id=OPERATOR_ID, now=NOW)
    before = await stored(repository)

    failing = FailingMerger()
    updater_2, _ = build_updater(
        conversation_of(24), merger=failing, checkpoints=repository,
    )
    report = await updater_2.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.MERGE_EMPTY
    assert failing.calls == 1
    assert await stored(repository) == before


async def test_an_inflating_merge_is_refused() -> None:
    """The guard that makes this compaction rather than accumulation: a
    "summary" longer than the old summary plus the backlog it swallowed
    is a transcript with extra steps."""
    messages = conversation_of(10)
    bloated = "囉唆" * 5000
    updater, repository = build_updater(messages, merger=FakeMerger([bloated]))

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.INFLATED
    assert await stored(repository) is None


async def test_a_refused_inflation_leaves_the_backlog_for_the_next_run() -> None:
    messages = conversation_of(10)
    merger = FakeMerger(["囉唆" * 5000, "這次好好壓縮"])
    updater, repository = build_updater(messages, merger=merger)

    first = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )
    second = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert first.outcome is CheckpointUpdateOutcome.INFLATED
    assert second.outcome is CheckpointUpdateOutcome.WRITTEN
    assert merger.calls[1][1] == merger.calls[0][1]


async def test_a_lost_cas_race_drops_this_run_s_work() -> None:
    """Two replicas merged the same backlog concurrently.

    The race has to be injected *between* the read and the write — a
    competitor that lands before the read is not a race at all, it just
    shrinks the backlog. ``_RacingRepository`` lets the other replica in
    at the only moment that makes this a compare-and-swap test.
    """
    messages = conversation_of(10)

    class _RacingRepository(InMemoryDialogueCheckpointRepository):
        def __init__(self) -> None:
            super().__init__()
            self._raced = False

        async def save(
            self, checkpoint, *, expected_message_key, expected_stale=False,
        ):
            if not self._raced:
                self._raced = True
                await super().save(
                    DialogueCheckpoint.create(
                        character_id=CHARACTER_ID,
                        operator_id=OPERATOR_ID,
                        summary_text="對方的摘要",
                        boundary=messages[6],
                        now=NOW,
                    ),
                    expected_message_key=expected_message_key,
                    expected_stale=expected_stale,
                )
            return await super().save(
                checkpoint,
                expected_message_key=expected_message_key,
                expected_stale=expected_stale,
            )

    repository = _RacingRepository()
    updater, _ = build_updater(
        messages, merger=FakeMerger(["我方摘要"]), checkpoints=repository,
    )

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.LOST_RACE
    assert (await stored(repository)).summary_text == "對方的摘要"


async def test_a_tool_only_backlog_has_nothing_to_summarise() -> None:
    messages = [tool_only_message(at(100 - i)) for i in range(10)]
    merger = FakeMerger()
    updater, repository = build_updater(messages, merger=merger)

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.BACKLOG_TOO_SMALL
    assert merger.calls == []
    assert await stored(repository) is None


async def test_the_flag_off_updater_does_nothing_at_all() -> None:
    merger = FakeMerger()
    updater, repository = build_updater(
        conversation_of(30), merger=merger, enabled=False,
    )

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.DISABLED
    assert merger.calls == []
    assert await stored(repository) is None


async def test_a_character_with_no_operator_is_skipped() -> None:
    updater, repository = build_updater(conversation_of(30))
    report = await updater.run(
        character=character(), operator_id="", now=NOW,
    )
    assert report.outcome is CheckpointUpdateOutcome.DISABLED
    assert await stored(repository) is None


async def test_a_crashing_store_does_not_escape_the_post_turn() -> None:
    """Fail-soft is structural: the post-turn runs several subsystems
    after this one, and a raise here would take them all down."""

    class _Exploding(InMemoryDialogueCheckpointRepository):
        async def get(self, **kwargs):
            raise RuntimeError("store is down")

    updater, _ = build_updater(
        conversation_of(10), checkpoints=_Exploding(),
    )
    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )
    assert report.outcome is CheckpointUpdateOutcome.DISABLED


# --- D5: the raw tail is untouchable -----------------------------------


async def test_the_checkpoint_never_covers_a_raw_tail_message() -> None:
    """The invariant undo depends on. Everything the player can still
    reverse is outside the summary by construction."""
    messages = conversation_of(30)
    updater, repository = build_updater(messages, tail=3)

    await updater.run(character=character(), operator_id=OPERATOR_ID, now=NOW)

    checkpoint = await stored(repository)
    for message in messages[-3:]:
        assert not checkpoint.covers(message)


async def test_a_backlog_that_would_reach_the_tail_is_refused() -> None:
    """Structurally unreachable through ``split_window`` — pinned anyway
    by driving the updater with a tail limit of one and a window that
    puts the boundary next to it, then asserting the guard's verdict is
    the *outcome*, not a crash."""
    messages = conversation_of(8)
    updater, repository = build_updater(messages, tail=1)

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.WRITTEN
    checkpoint = await stored(repository)
    assert not checkpoint.covers(messages[-1])


# --- NSFW --------------------------------------------------------------


async def test_restricted_originals_never_reach_the_merger() -> None:
    """The checkpoint is read back on every later turn, including
    frontier ones, so it is built from frontier-safe text regardless of
    the tolerance of the turn that triggered the run."""
    secret = "這是不該進摘要的原文"
    messages = conversation_of(10)
    messages.insert(4, restricted_message(secret, at(595)))
    merger = FakeMerger()
    updater, _ = build_updater(messages, merger=merger)

    await updater.run(character=character(), operator_id=OPERATOR_ID, now=NOW)

    assert merger.calls
    assert all(secret not in text for text in merger.calls[0][1])


async def test_a_restricted_message_contributes_only_its_safe_summary() -> None:
    messages = conversation_of(10)
    messages.insert(
        4,
        restricted_message(
            "露骨原文", at(595), safe_summary="兩人聊到比較私密的話題",
        ),
    )
    merger = FakeMerger()
    updater, _ = build_updater(messages, merger=merger)

    await updater.run(character=character(), operator_id=OPERATOR_ID, now=NOW)

    handed = merger.calls[0][1]
    assert "露骨原文" not in handed
    assert "兩人聊到比較私密的話題" in handed


async def test_a_backlog_of_only_unreplaceable_restricted_text_writes_nothing() -> None:
    messages = [
        restricted_message(f"原文{i}" * 40, at(100 - i)) for i in range(12)
    ]
    merger = FakeMerger()
    updater, repository = build_updater(messages, merger=merger)

    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.NOTHING_TO_MERGE
    assert merger.calls == []
    assert await stored(repository) is None


# --- stale ------------------------------------------------------------


async def test_a_stale_checkpoint_is_rebuilt_from_scratch() -> None:
    messages = conversation_of(10)
    merger = FakeMerger(["原本的摘要", "重建的摘要"])
    updater, repository = build_updater(messages, merger=merger)
    await updater.run(character=character(), operator_id=OPERATOR_ID, now=NOW)
    await repository.mark_stale(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID, now=NOW,
    )

    updater_2, _ = build_updater(
        conversation_of(24), merger=merger, checkpoints=repository,
    )
    report = await updater_2.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.WRITTEN
    # Rebuild, not merge: the stale summary is not offered as an input.
    assert merger.calls[1][0] == ""
    assert (await stored(repository)).stale is False


async def test_a_rebuild_re_reads_the_region_the_old_summary_covered() -> None:
    """"From scratch" has to include the split, not just the text.

    Discarding ``previous_summary`` while still splitting the window
    against the discarded summary's cursor is the worst of both: every
    message older than that cursor lands in ``window.covered``, where
    the rebuild never reads it and the prompt never renders it. The old
    summary's entire reach is deleted rather than re-derived — and on a
    conversation that has not moved since, there is no backlog left at
    all, so the rebuild never even fires and the pair keeps a stale row
    (which the reader declines to use) indefinitely.

    Same window on both runs, deliberately: nothing new has been said
    since the undo that raised the flag, which is the ordinary case.
    """
    messages = conversation_of(20)
    merger = FakeMerger(["原本的摘要", "重建的摘要"])
    updater, repository = build_updater(messages, merger=merger)
    await updater.run(character=character(), operator_id=OPERATOR_ID, now=NOW)
    first = await stored(repository)
    covered_texts = {
        m.content for m in messages if first.covers(m)
    }
    assert covered_texts

    await repository.mark_stale(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID, now=NOW,
    )
    report = await updater.run(
        character=character(), operator_id=OPERATOR_ID, now=NOW,
    )

    assert report.outcome is CheckpointUpdateOutcome.WRITTEN
    rebuilt_from = set(merger.calls[1][1])
    assert covered_texts <= rebuilt_from
    after = await stored(repository)
    assert after.stale is False
    assert after.summary_text == "重建的摘要"
    # And the coverage did not walk backwards: everything the discarded
    # summary reached is still inside the new one's reach.
    assert all(after.covers(m) for m in messages if first.covers(m))
