"""ProactiveDispatcher forwards a recent-dialogue summary into the decider context.

The summary comes from running the wired ``DialogueSummarizerPort``
against the latest web conversation (tool-only turns filtered). When no
summarizer is configured, ``recent_dialogue_summary`` stays empty — the
decider prompt treats empty as "no context" and skips that section.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.contracts.proactive import (
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageKind,
    MessageRole,
)
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.proactive.heuristic_gate import HeuristicProactiveGate
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from tests.unit._messaging_harness import (
    build_messaging_harness,
    create_character,
    create_telegram_account,
)


class _CapturingDecider(ProactiveDeciderPort):
    def __init__(self) -> None:
        self.last_context: ProactiveContext | None = None

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.last_context = context
        return ProactiveDecision(False, "inspection only", None)


class _RecordingSummarizer:
    def __init__(self, output: str) -> None:
        self.output = output
        self.calls: list[list[Message]] = []

    async def summarize(self, *, character, messages):  # noqa: ANN001
        self.calls.append(list(messages))
        return self.output


async def _prepare_harness_with_enabled_character():
    harness = build_messaging_harness()
    dto = await create_character(harness)
    character = await harness.character_repository.get(dto.id)
    assert character is not None
    enabled = character.update(
        name=None, summary=None, personality=None, interests=None,
        speaking_style=None, boundaries=None, aspirations=None, appearance=None,
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=0, trust=60, energy=80,
            last_active_at=datetime.now(timezone.utc) - timedelta(hours=2),
        ),
        proactive_enabled=True,
    )
    await harness.character_repository.save(enabled)
    account = await create_telegram_account(harness, character_id=character.id)
    await harness.binding_repository.save(
        ChannelBinding.create(
            account_id=account.id, chat_ref="c1", accepts_proactive=True,
        ),
    )
    return harness, character


async def _seed_web_conversation(harness, character_id: str) -> None:
    convo = Conversation.start(character_id=character_id)
    convo = convo.append(Message(role=MessageRole.USER, content="今天有點悶"))
    convo = convo.append(Message(
        role=MessageRole.ASSISTANT, content="我陪你聊聊，先深呼吸一下",
    ))
    convo = convo.append(Message(
        role=MessageRole.ASSISTANT, content="",
        kind=MessageKind.TOOL_ONLY,
    ))
    await harness.conversation_repository.save(convo)


def _build_dispatcher(harness, *, decider, dialogue_summarizer):
    return ProactiveDispatcher(
        character_repository=harness.character_repository,
        conversation_repository=harness.conversation_repository,
        account_repository=harness.account_repository,
        binding_repository=harness.binding_repository,
        attempt_repository=InMemoryProactiveAttemptRepository(),
        gate=HeuristicProactiveGate(
            local_tz=timezone.utc, quiet_hour_start=0, quiet_hour_end=0,
        ),
        decider=decider,
        adapters={
            Platform.TELEGRAM: harness.telegram_adapter,
            Platform.LINE: harness.line_adapter,
        },
        dialogue_summarizer=dialogue_summarizer,
    )


@pytest.mark.asyncio
async def test_dispatcher_threads_dialogue_summary_into_context() -> None:
    harness, character = await _prepare_harness_with_enabled_character()
    await _seed_web_conversation(harness, character.id)

    decider = _CapturingDecider()
    summarizer = _RecordingSummarizer(output="你剛陪對方處理心情低落")
    dispatcher = _build_dispatcher(
        harness, decider=decider, dialogue_summarizer=summarizer,
    )

    await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert decider.last_context is not None
    assert decider.last_context.recent_dialogue_summary == "你剛陪對方處理心情低落"
    # Tool-only turn got filtered out before reaching the summarizer.
    assert len(summarizer.calls) == 1
    assert all(m.kind is MessageKind.CHAT for m in summarizer.calls[0])


@pytest.mark.asyncio
async def test_dispatcher_without_summarizer_passes_empty_summary() -> None:
    harness, character = await _prepare_harness_with_enabled_character()
    await _seed_web_conversation(harness, character.id)

    decider = _CapturingDecider()
    dispatcher = _build_dispatcher(
        harness, decider=decider, dialogue_summarizer=None,
    )

    await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert decider.last_context is not None
    assert decider.last_context.recent_dialogue_summary == ""


@pytest.mark.asyncio
async def test_dispatcher_with_no_conversation_passes_empty_summary() -> None:
    harness, character = await _prepare_harness_with_enabled_character()
    # deliberately no conversation seeded

    decider = _CapturingDecider()
    summarizer = _RecordingSummarizer(output="should not be called")
    dispatcher = _build_dispatcher(
        harness, decider=decider, dialogue_summarizer=summarizer,
    )

    await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert decider.last_context is not None
    assert decider.last_context.recent_dialogue_summary == ""
    assert summarizer.calls == []
