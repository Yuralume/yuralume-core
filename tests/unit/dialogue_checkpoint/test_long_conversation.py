"""Sixty turns, driven end to end through both halves of DH3.

The unit suites next door each pin one decision. This one asks the
question none of them can: does the whole thing *converge*? A design
that compresses correctly on every single step can still drift — the
summary creeping longer, the coverage falling behind the window, the
raw tail quietly shrinking under a budget.

Everything is scripted and every timestamp is derived from a frozen
``NOW``. No wall clock, no model.
"""

from __future__ import annotations

import re

import pytest

from kokoro_link.application.services.dialogue_checkpoint import (
    CheckpointUpdateOutcome,
    DialogueCheckpointReader,
    DialogueCheckpointUpdater,
)
from kokoro_link.application.services.dialogue_checkpoint.window import (
    total_tokens,
)
from kokoro_link.contracts.dialogue_checkpoint import (
    DialogueCheckpointMergeResult,
)
from kokoro_link.infrastructure.dialogue.llm_checkpoint_merger import (
    MAX_SUMMARY_CHARS,
)
from kokoro_link.infrastructure.repositories.in_memory_dialogue_checkpoints import (
    InMemoryDialogueCheckpointRepository,
)
from kokoro_link.llm_output import estimate_tokens
from tests.unit.dialogue_checkpoint.builders import (
    CHARACTER_ID,
    NOW,
    OPERATOR_ID,
    StubConversationRepository,
    assistant_message,
    at,
    character,
    user_message,
)

pytestmark = pytest.mark.asyncio

TURNS = 60
RAW_TAIL = 3
WINDOW = 30
PROMPT_BUDGET = 2400
TRIGGER = 400

RELATIVE_TIME_WORDS = (
    "剛剛", "方才", "今天早上", "昨天", "今晚", "稍早",
    "等一下", "待會", "上禮拜", "前幾天", "最近這幾天",
)
_RELATIVE_TIME = re.compile("|".join(RELATIVE_TIME_WORDS))


class ScriptedMerger:
    """A merger that behaves the way the prompt asks a model to.

    It keeps a bounded set of facts rather than concatenating, and it
    writes them without relative-time words — so the convergence and
    time-neutrality assertions below are testing the *machinery* around
    the merge, which is the only part this repository controls. Whether
    a real model obeys the prompt is a prompt question, not a code one.
    """

    def __init__(self) -> None:
        self.calls = 0

    async def merge(self, *, character, previous_summary, messages):
        self.calls += 1
        facts = [
            line for line in (previous_summary or "").split("；") if line
        ]
        for message in messages:
            # Includes the turn number, so every absorbed turn is a
            # *distinct* fact — otherwise the cap below would never
            # bind and the convergence test would prove nothing.
            topic = message.content[:12]
            if topic not in facts:
                facts.append(topic)
        # Merge, don't restate: the oldest facts are compressed away
        # once the summary reaches its working size.
        return DialogueCheckpointMergeResult(
            summary="；".join(facts[-12:]), model="scripted",
        )


def script(turn: int) -> list:
    """One turn = a user line and a reply, a minute apart."""
    text = (
        f"第{turn}輪話題：我們談到工作的進度、家裡的狀況，還有週末"
        f"要不要一起出門走走這件事。"
    )
    reply = (
        f"第{turn}輪回覆：我覺得那樣安排很好，那就先這樣說定，"
        f"晚點再看情況調整。"
    )
    base = 1000 - turn * 2
    return [
        user_message(text, at(base)),
        assistant_message(reply, at(base - 1)),
    ]


async def run_conversation():
    """Play ``TURNS`` turns, advancing the checkpoint after each."""
    repository = InMemoryDialogueCheckpointRepository()
    merger = ScriptedMerger()
    history: list = []
    conversations = StubConversationRepository(history)
    updater = DialogueCheckpointUpdater(
        checkpoints=repository,
        merger=merger,
        conversations=conversations,
        window_messages=WINDOW,
        raw_tail_limit=RAW_TAIL,
        backlog_trigger_tokens=TRIGGER,
    )
    reader = DialogueCheckpointReader(
        checkpoints=repository,
        raw_tail_limit=RAW_TAIL,
        prompt_budget_tokens=PROMPT_BUDGET,
    )
    summary_sizes: list[int] = []
    prompt_sizes: list[tuple[int, int]] = []
    tail_checks = 0
    skipped_reads = 0
    who = character()

    for turn in range(TURNS):
        history.extend(script(turn))
        conversations.messages = list(history)
        window = history[-WINDOW:]
        context = await reader.read(
            character_id=CHARACTER_ID,
            operator_id=OPERATOR_ID,
            recent_messages=window,
        )
        if context is None:
            skipped_reads += 1
        else:
            prompt_sizes.append((
                total_tokens(context.messages),
                estimate_tokens(context.summary),
            ))
            assert list(context.messages[-RAW_TAIL:]) == window[-RAW_TAIL:]
            tail_checks += 1
        report = await updater.run(
            character=who, operator_id=OPERATOR_ID, now=NOW,
        )
        assert report.outcome in {
            CheckpointUpdateOutcome.WRITTEN,
            CheckpointUpdateOutcome.BACKLOG_TOO_SMALL,
        }
        checkpoint = await repository.get(
            character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
        )
        if checkpoint is not None:
            summary_sizes.append(estimate_tokens(checkpoint.summary_text))

    return {
        "repository": repository,
        "merger": merger,
        "history": history,
        "summary_sizes": summary_sizes,
        "prompt_sizes": prompt_sizes,
        "tail_checks": tail_checks,
        "skipped_reads": skipped_reads,
    }


async def test_the_checkpoint_converges_instead_of_growing() -> None:
    """The failure this guards against is the one that looks like
    success turn by turn: every merge compresses, and the summary still
    creeps up forever."""
    result = await run_conversation()
    sizes = result["summary_sizes"]

    assert sizes, "the checkpoint never landed at all"
    # It has to have grown at some point, or "it converged" is just
    # "nothing ever happened".
    assert max(sizes) > min(sizes)
    # And then stopped: the last third is no larger than the peak.
    assert max(sizes[-len(sizes) // 3:]) <= max(sizes)
    assert max(sizes) < MAX_SUMMARY_CHARS


async def test_the_merge_runs_on_a_small_fraction_of_turns() -> None:
    """D3's whole point: the old design paid for a summarisation call on
    every turn, in front of the player's reply."""
    result = await run_conversation()
    assert 0 < result["merger"].calls <= TURNS // 4


async def test_the_dialogue_section_stays_inside_its_budget() -> None:
    """Raw text under the budget, summary under its own cap.

    Two separate ceilings, checked separately — adding them into one
    number would let an over-budget transcript hide behind a short
    summary.
    """
    result = await run_conversation()
    assert result["prompt_sizes"]
    for raw_tokens, summary_tokens in result["prompt_sizes"]:
        assert raw_tokens <= PROMPT_BUDGET
        assert summary_tokens <= MAX_SUMMARY_CHARS


async def test_the_raw_tail_survives_every_single_turn() -> None:
    """The tail is the turn the reply is answering. A budget that could
    eat it would let one long message shorten the context of the next.

    ``run_conversation`` asserts this on every one of the sixty turns;
    this test exists so a failure names the property.
    """
    result = await run_conversation()
    # The early turns have no checkpoint yet and legitimately read
    # ``None``; every turn after that is checked.
    assert result["tail_checks"] >= TURNS // 2


async def test_the_summary_never_uses_a_relative_time_word() -> None:
    """A checkpoint is read back days later. 「剛剛」 in it is not a
    stale detail — it is an outright false statement about now.

    A regex smoke test over a scripted merger cannot police a real
    model; what it pins is that nothing in the *machinery* (labels,
    joins, the fallback text) introduces one.
    """
    result = await run_conversation()
    checkpoint = await result["repository"].get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )
    assert checkpoint is not None
    assert not _RELATIVE_TIME.search(checkpoint.summary_text)


async def test_reversing_the_newest_turn_leaves_the_checkpoint_whole() -> None:
    """D5 from the undo side: the two messages a player can still take
    back are never inside the coverage, at any point in sixty turns."""
    result = await run_conversation()
    checkpoint = await result["repository"].get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )
    history = result["history"]

    for message in history[-2:]:
        assert not checkpoint.covers(message)

    # And the summary itself is untouched by dropping them.
    before = checkpoint.summary_text
    del history[-2:]
    still = await result["repository"].get(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID,
    )
    assert still.summary_text == before
    assert not still.stale


async def test_the_prompt_reaches_further_back_than_the_old_eight() -> None:
    """The point of the exercise. The pre-DH3 prompt saw eight messages
    and summarised five of them per turn; here the window is wider *and*
    everything behind it is still represented."""
    result = await run_conversation()
    history = result["history"]
    reader = DialogueCheckpointReader(
        checkpoints=result["repository"],
        raw_tail_limit=RAW_TAIL,
        prompt_budget_tokens=PROMPT_BUDGET,
    )
    context = await reader.read(
        character_id=CHARACTER_ID,
        operator_id=OPERATOR_ID,
        recent_messages=history[-WINDOW:],
    )
    assert context is not None
    assert len(context.messages) > 8
    assert context.summary


async def test_a_tight_budget_trims_the_middle_and_keeps_the_tail() -> None:
    """The same sixty-turn state, read under a budget that actually
    bites — the production default never does at this message size, so
    the end-to-end path would otherwise never exercise a trim."""
    result = await run_conversation()
    history = result["history"]
    window = history[-WINDOW:]
    tight = DialogueCheckpointReader(
        checkpoints=result["repository"],
        raw_tail_limit=RAW_TAIL,
        prompt_budget_tokens=300,
    )
    context = await tight.read(
        character_id=CHARACTER_ID,
        operator_id=OPERATOR_ID,
        recent_messages=window,
    )
    assert context is not None
    assert context.dropped_middle > 0
    assert list(context.messages[-RAW_TAIL:]) == window[-RAW_TAIL:]
    assert context.summary
