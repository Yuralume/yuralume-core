"""The prompt-side read: what the dialogue section contains, and when
the reader steps aside for the pre-DH3 path.

Every ``None`` here is a *narrowing* failure. There is no input to this
reader that makes the prompt longer, which is the correction DH3 makes
to the summariser it replaces — that one fell back to the complete raw
message list, so a flaky provider lengthened the prompt exactly when it
could least afford to.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.dialogue_checkpoint import (
    DialogueCheckpointReader,
)
from kokoro_link.application.services.dialogue_checkpoint.window import (
    total_tokens,
)
from kokoro_link.domain.entities.dialogue_checkpoint import DialogueCheckpoint
from kokoro_link.infrastructure.repositories.in_memory_dialogue_checkpoints import (
    InMemoryDialogueCheckpointRepository,
)
from tests.unit.dialogue_checkpoint.builders import (
    CHARACTER_ID,
    NOW,
    OPERATOR_ID,
    conversation_of,
)

pytestmark = pytest.mark.asyncio


async def _repository_with(messages, index: int, *, summary: str = "累積摘要"):
    repository = InMemoryDialogueCheckpointRepository()
    await repository.save(
        DialogueCheckpoint.create(
            character_id=CHARACTER_ID,
            operator_id=OPERATOR_ID,
            summary_text=summary,
            boundary=messages[index],
            now=NOW,
        ),
        expected_message_key=None,
    )
    return repository


def _reader(repository, *, budget: int = 100_000, tail: int = 3):
    return DialogueCheckpointReader(
        checkpoints=repository,
        raw_tail_limit=tail,
        prompt_budget_tokens=budget,
    )


async def _read(reader, messages):
    return await reader.read(
        character_id=CHARACTER_ID,
        operator_id=OPERATOR_ID,
        recent_messages=messages,
    )


# --- stepping aside ----------------------------------------------------


async def test_no_checkpoint_yet_hands_back_to_the_old_path() -> None:
    reader = _reader(InMemoryDialogueCheckpointRepository())
    assert await _read(reader, conversation_of(20)) is None


async def test_an_empty_summary_hands_back_to_the_old_path() -> None:
    messages = conversation_of(20)
    repository = await _repository_with(messages, 10, summary="")
    assert await _read(_reader(repository), messages) is None


async def test_a_stale_checkpoint_is_not_read_into_the_prompt() -> None:
    """The flag says "part of this is no longer true", and the usual
    reason it is set is that the player reversed a turn.

    Nothing rebuilds the summary until the *next* merge earns an LLM
    call, which is many turns away on a quiet conversation. A reader
    that kept using the row for those turns would have the character
    going on referring to something the player took back — the one
    failure an undo exists to prevent, arriving through the one door
    that has no un-merge. Handing back to the raw path costs context and
    keeps the promise.
    """
    messages = conversation_of(20)
    repository = await _repository_with(
        messages, 10, summary="她說她週五要去看醫生",
    )
    live = await _read(_reader(repository), messages)
    assert live is not None and live.summary

    await repository.mark_stale(
        character_id=CHARACTER_ID, operator_id=OPERATOR_ID, now=NOW,
    )

    assert await _read(_reader(repository), messages) is None


async def test_a_character_with_no_operator_hands_back() -> None:
    messages = conversation_of(20)
    repository = await _repository_with(messages, 10)
    result = await _reader(repository).read(
        character_id=CHARACTER_ID, operator_id="", recent_messages=messages,
    )
    assert result is None


async def test_an_unreachable_store_hands_back_rather_than_raising() -> None:
    """A store that will not answer is indistinguishable from one with
    no row — and a chat turn must not 500 over a summary."""

    class _Exploding(InMemoryDialogueCheckpointRepository):
        async def get(self, **kwargs):
            raise RuntimeError("store is down")

    assert await _read(_reader(_Exploding()), conversation_of(20)) is None


# --- the assembled section --------------------------------------------


async def test_the_summary_replaces_the_covered_messages() -> None:
    messages = conversation_of(20)
    repository = await _repository_with(messages, 11)
    result = await _read(_reader(repository), messages)

    assert result is not None
    assert result.summary == "累積摘要"
    assert list(result.messages) == messages[12:]
    covered = {m.content for m in messages[:12]}
    assert not covered & {m.content for m in result.messages}


async def test_the_raw_tail_is_always_present() -> None:
    messages = conversation_of(20)
    repository = await _repository_with(messages, 11)
    result = await _read(_reader(repository, budget=1), messages)

    assert result is not None
    assert list(result.messages) == messages[-3:]


async def test_the_budget_trims_the_middle_oldest_first() -> None:
    messages = conversation_of(20)
    repository = await _repository_with(messages, 5)
    budget = total_tokens(messages[-6:])

    result = await _read(_reader(repository, budget=budget), messages)

    assert result is not None
    assert list(result.messages) == messages[-6:]
    assert result.dropped_middle == len(messages[6:-6])


async def test_a_generous_budget_keeps_the_whole_middle() -> None:
    messages = conversation_of(20)
    repository = await _repository_with(messages, 5)
    result = await _read(_reader(repository), messages)

    assert result is not None
    assert result.dropped_middle == 0
    assert list(result.messages) == messages[6:]


async def test_a_checkpoint_older_than_the_window_still_renders() -> None:
    """The window has scrolled entirely past the coverage. Nothing is
    covered *in this window*, but the summary is still the character's
    memory of what came before it."""
    messages = conversation_of(20)
    older = conversation_of(4, oldest_minutes_before=5000)
    repository = await _repository_with(older, 3)

    result = await _read(_reader(repository), messages)

    assert result is not None
    assert result.summary == "累積摘要"
    assert list(result.messages) == messages
