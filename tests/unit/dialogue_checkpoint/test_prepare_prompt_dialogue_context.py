"""Characterization of ``ChatService._prepare_prompt_dialogue_context``.

The flag-off half is the DH3 regression oracle: with no checkpoint
wired, this method must behave exactly as it did before the ticket,
including the three branches its failure path splits into —

1. the summary came back non-empty → raw tail plus the summary;
2. the summary was empty **and** the window holds restricted messages
   under the frontier tolerance → raw tail plus *nothing*, because
   falling back to the full list would put restricted originals into a
   frontier prompt;
3. the summary was empty and there is nothing restricted → the whole
   raw list, unsummarised (the old fail-soft "no context loss").

Branch 3 is the one DH3 reverses when the flag is on: a failed merge
must not make the prompt *longer*. Both behaviours are pinned here, side
by side, so the difference is legible rather than inferred.
"""

from __future__ import annotations

import pytest

from kokoro_link.application.services.chat_service import (
    _PROMPT_RAW_RECENT_MESSAGE_LIMIT,
    _RECENT_MESSAGE_LIMIT,
    ChatService,
)
from kokoro_link.application.services.dialogue_checkpoint import (
    DialogueCheckpointReader,
)
from kokoro_link.domain.entities.dialogue_checkpoint import DialogueCheckpoint
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_COMMUNITY,
    CONTENT_TOLERANCE_FRONTIER,
)
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import (
    NullPostTurnProcessor,
)
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_dialogue_checkpoints import (
    InMemoryDialogueCheckpointRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

from tests.unit.dialogue_checkpoint.builders import (
    CHARACTER_ID,
    NOW,
    OPERATOR_ID,
    at,
    character,
    conversation_of,
    restricted_message,
)

pytestmark = pytest.mark.asyncio


class _StubModel:
    provider_id = "stub"
    supports_vision = False

    async def generate(self, prompt, *, image_urls=(), model=None):  # noqa: ANN001
        return "ok"

    async def list_models(self) -> list[str]:
        return ["stub"]


class _Summarizer:
    """The pre-DH3 ``DialogueSummarizerPort``, scripted."""

    def __init__(self, summary: str) -> None:
        self._summary = summary
        self.calls = 0
        self.handed: list[list] = []
        """What it was actually asked to summarise. The *size* of this is
        the request-path cost of the pre-DH3 fallback, and it is the only
        place a regression in that cost shows — the returned summary
        looks identical whether five messages went in or twenty-seven."""

    async def summarize(self, *, character, messages, now=None, local_tz=None):
        self.calls += 1
        self.handed.append(list(messages))
        return self._summary


def _service(*, summarizer=None, checkpoint_reader=None, window=None):
    registry = InMemoryChatModelRegistry(default_provider_id="stub")
    registry.register(_StubModel())
    return ChatService(
        character_repository=InMemoryCharacterRepository(),
        conversation_repository=InMemoryConversationRepository(),
        memory_repository=InMemoryMemoryRepository(),
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        dialogue_summarizer=summarizer,
        dialogue_checkpoint_reader=checkpoint_reader,
        dialogue_window_limit=window,
    )


async def _prepare(service, messages, *, tolerance=CONTENT_TOLERANCE_FRONTIER):
    return await service._prepare_prompt_dialogue_context(
        character=character(),
        recent_messages=list(messages),
        content_tolerance=tolerance,
    )


# --- flag off: the three branches, unchanged ---------------------------


async def test_flag_off_a_short_window_is_returned_whole() -> None:
    summarizer = _Summarizer("摘要")
    service = _service(summarizer=summarizer)
    messages = conversation_of(_PROMPT_RAW_RECENT_MESSAGE_LIMIT)

    result = await _prepare(service, messages)

    assert result == (messages, "")
    assert summarizer.calls == 0


async def test_flag_off_branch_1_a_summary_replaces_the_older_turns() -> None:
    service = _service(summarizer=_Summarizer("較早的摘要"))
    messages = conversation_of(8)

    tail, summary = await _prepare(service, messages)

    assert tail == messages[-_PROMPT_RAW_RECENT_MESSAGE_LIMIT:]
    assert summary == "較早的摘要"


async def test_flag_off_branch_2_an_empty_summary_over_restricted_text_narrows() -> None:
    """Restricted originals must not re-enter a frontier prompt through
    the fallback. The window is narrowed to the raw tail and the older
    turns are simply lost — the deliberate, pre-DH3 behaviour."""
    service = _service(summarizer=_Summarizer(""))
    messages = conversation_of(8)
    messages[1] = restricted_message("露骨原文", at(599))

    tail, summary = await _prepare(service, messages)

    assert tail == messages[-_PROMPT_RAW_RECENT_MESSAGE_LIMIT:]
    assert summary == ""
    assert all("露骨原文" not in m.content for m in tail)


async def test_flag_off_branch_2_does_not_narrow_under_community_tolerance() -> None:
    """The narrowing exists for the frontier frontier only; a community
    route may keep the raw text, so the fallback stays wide."""
    service = _service(summarizer=_Summarizer(""))
    messages = conversation_of(8)
    messages[1] = restricted_message("露骨原文", at(599))

    result = await _prepare(
        service, messages, tolerance=CONTENT_TOLERANCE_COMMUNITY,
    )

    assert result == (messages, "")


async def test_flag_off_branch_3_an_empty_summary_falls_back_to_everything() -> None:
    """The old failure direction, pinned as it is: no summary means the
    prompt gets *longer*, not shorter. DH3 reverses this — see the
    flag-on test below."""
    service = _service(summarizer=_Summarizer(""))
    messages = conversation_of(8)

    result = await _prepare(service, messages)

    assert result == (messages, "")


async def test_flag_off_no_summarizer_at_all_still_falls_back() -> None:
    service = _service(summarizer=None)
    messages = conversation_of(8)
    assert await _prepare(service, messages) == (messages, "")


async def test_flag_off_a_raising_summarizer_is_swallowed() -> None:
    class _Exploding:
        async def summarize(self, *, character, messages, now=None, local_tz=None):
            raise RuntimeError("provider down")

    service = _service(summarizer=_Exploding())
    messages = conversation_of(8)
    assert await _prepare(service, messages) == (messages, "")


async def test_flag_off_keeps_the_eight_message_window() -> None:
    """The window constant is untouched unless the flag widens it."""
    assert _service()._dialogue_window_limit == _RECENT_MESSAGE_LIMIT


# --- flag on: the checkpoint takes over --------------------------------


async def _reader_with(messages, index: int):
    repository = InMemoryDialogueCheckpointRepository()
    await repository.save(
        DialogueCheckpoint.create(
            character_id=CHARACTER_ID,
            operator_id=OPERATOR_ID,
            summary_text="累積摘要",
            boundary=messages[index],
            now=NOW,
        ),
        expected_message_key=None,
    )
    return DialogueCheckpointReader(
        checkpoints=repository,
        raw_tail_limit=_PROMPT_RAW_RECENT_MESSAGE_LIMIT,
        prompt_budget_tokens=100_000,
    )


async def test_flag_on_the_checkpoint_supplies_the_summary() -> None:
    messages = conversation_of(20)
    service = _service(
        summarizer=_Summarizer("不該被用到"),
        checkpoint_reader=await _reader_with(messages, 9),
        window=30,
    )

    prompt_messages, summary = await _prepare(service, messages)

    assert summary == "累積摘要"
    assert prompt_messages == messages[10:]


async def test_flag_on_never_calls_the_old_summarizer() -> None:
    """One summarisation mechanism per turn, not two."""
    messages = conversation_of(20)
    summarizer = _Summarizer("不該被用到")
    service = _service(
        summarizer=summarizer,
        checkpoint_reader=await _reader_with(messages, 9),
        window=30,
    )

    await _prepare(service, messages)

    assert summarizer.calls == 0


async def test_flag_on_with_no_checkpoint_yet_runs_the_old_path() -> None:
    """Degradation, not a second behaviour: before the first merge there
    is no summary of the older turns, so showing them raw is the only
    way not to lose them."""
    summarizer = _Summarizer("較早的摘要")
    service = _service(
        summarizer=summarizer,
        checkpoint_reader=DialogueCheckpointReader(
            checkpoints=InMemoryDialogueCheckpointRepository(),
            raw_tail_limit=_PROMPT_RAW_RECENT_MESSAGE_LIMIT,
            prompt_budget_tokens=100_000,
        ),
        window=30,
    )
    messages = conversation_of(8)

    tail, summary = await _prepare(service, messages)

    assert summarizer.calls == 1
    assert summary == "較早的摘要"
    assert tail == messages[-_PROMPT_RAW_RECENT_MESSAGE_LIMIT:]


async def test_flag_on_widens_the_loaded_window() -> None:
    assert _service(window=30)._dialogue_window_limit == 30


# --- flag on, no checkpoint yet: the fallback must not get expensive ---


async def test_flag_on_with_no_checkpoint_summarises_the_old_number_of_turns() -> None:
    """The regression this pins is a cost regression, and it hides
    behind a correct-looking result.

    With the flag on the caller loads a much wider window, because the
    checkpoint is supposed to be carrying everything behind the tail.
    Before the first merge there is no checkpoint, so control lands in
    the pre-DH3 branch — and that branch hands *everything but the last
    three messages* to a per-turn LLM summariser on the request path. At
    a 30-message window that is 27 messages summarised in front of the
    player's reply, every turn, against the five it was before: five
    times the pre-DH3 cost, in the exact situation DH3 exists to remove.

    The output is unremarkable either way, so only the summariser's
    input shows it.
    """
    summarizer = _Summarizer("較早的摘要")
    service = _service(
        summarizer=summarizer,
        checkpoint_reader=DialogueCheckpointReader(
            checkpoints=InMemoryDialogueCheckpointRepository(),
            raw_tail_limit=_PROMPT_RAW_RECENT_MESSAGE_LIMIT,
            prompt_budget_tokens=100_000,
        ),
        window=30,
    )
    messages = conversation_of(30)

    tail, summary = await _prepare(service, messages)

    assert summarizer.calls == 1
    handed = summarizer.handed[0]
    assert len(handed) == (
        _RECENT_MESSAGE_LIMIT - _PROMPT_RAW_RECENT_MESSAGE_LIMIT
    )
    # And it is the *newest* five, not the oldest five of the wide window.
    assert handed == messages[-_RECENT_MESSAGE_LIMIT:-
                              _PROMPT_RAW_RECENT_MESSAGE_LIMIT]
    assert tail == messages[-_PROMPT_RAW_RECENT_MESSAGE_LIMIT:]
    assert summary == "較早的摘要"


async def test_flag_on_a_failed_fallback_summary_does_not_widen_past_the_old_window() -> None:
    """The old fail-soft direction is "show the raw list instead". It is
    kept, but it must not now mean a raw list nearly four times longer
    than the one it was written for."""
    service = _service(
        summarizer=_Summarizer(""),
        checkpoint_reader=DialogueCheckpointReader(
            checkpoints=InMemoryDialogueCheckpointRepository(),
            raw_tail_limit=_PROMPT_RAW_RECENT_MESSAGE_LIMIT,
            prompt_budget_tokens=100_000,
        ),
        window=30,
    )
    messages = conversation_of(30)

    prompt_messages, summary = await _prepare(service, messages)

    assert summary == ""
    assert prompt_messages == messages[-_RECENT_MESSAGE_LIMIT:]


async def test_flag_off_the_narrowing_changes_nothing_at_all() -> None:
    """Identity, restated where it can break: with the flag off the
    loaded window is already at most ``_RECENT_MESSAGE_LIMIT``, so the
    narrowing is a no-op and every pre-DH3 branch is untouched."""
    summarizer = _Summarizer("較早的摘要")
    service = _service(summarizer=summarizer)
    messages = conversation_of(_RECENT_MESSAGE_LIMIT)

    tail, summary = await _prepare(service, messages)

    assert tail == messages[-_PROMPT_RAW_RECENT_MESSAGE_LIMIT:]
    assert summary == "較早的摘要"
    assert summarizer.handed[0] == messages[
        :-_PROMPT_RAW_RECENT_MESSAGE_LIMIT
    ]
