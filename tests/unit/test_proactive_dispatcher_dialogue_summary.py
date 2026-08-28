"""ProactiveDispatcher forwards a recent-dialogue summary into the decider context.

The summary comes from running the wired ``DialogueSummarizerPort``
against the latest web conversation (tool-only turns filtered). When no
summarizer is configured, ``recent_dialogue_summary`` stays empty — the
decider prompt treats empty as "no context" and skips that section.

TD (2026-08-26 incident: a ten-minute-old question resurfacing in a push
as 「昨天」) adds a second, deterministic anchor beside the summary. The
dispatcher now hands the summariser the tick's instant and the operator
zone, and appends the last few turns *verbatim with their timestamps*
to the same field. Both are asserted here: the summary alone is model
output and can lose the anchor however the template is worded, so the
raw tail is the part that cannot.
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
        self.anchors: list[tuple[object, object]] = []

    async def summarize(  # noqa: ANN001
        self, *, character, messages, now=None, local_tz=None,
    ):
        self.calls.append(list(messages))
        self.anchors.append((now, local_tz))
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
    assert decider.last_context.recent_dialogue_summary.startswith(
        "你剛陪對方處理心情低落",
    )
    # Tool-only turn got filtered out before reaching the summarizer.
    assert len(summarizer.calls) == 1
    assert all(m.kind is MessageKind.CHAT for m in summarizer.calls[0])


@pytest.mark.asyncio
async def test_dispatcher_hands_the_summarizer_the_tick_instant_and_zone() -> None:
    """The summariser cannot anchor turns it has no "now" to measure from.

    It fails soft to the UTC wall clock, which would have masked the
    incident in production while looking fine in tests — so the *caller*
    passing the real tick instant is pinned here explicitly.
    """
    harness, character = await _prepare_harness_with_enabled_character()
    await _seed_web_conversation(harness, character.id)

    summarizer = _RecordingSummarizer(output="你剛陪對方處理心情低落")
    dispatcher = _build_dispatcher(
        harness, decider=_CapturingDecider(), dialogue_summarizer=summarizer,
    )

    await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert len(summarizer.anchors) == 1
    now, local_tz = summarizer.anchors[0]
    assert isinstance(now, datetime)
    assert now.tzinfo is not None
    assert local_tz is not None


@pytest.mark.asyncio
async def test_decider_sees_raw_recent_turns_with_their_timestamps() -> None:
    """The deterministic half of the fix.

    A summary that drops the time anchor is still possible — it is model
    output. The last turns therefore also reach the decider verbatim,
    stamped, assembled in Python, so a misdated summary is contradicted
    by the material directly beneath it.
    """
    harness, character = await _prepare_harness_with_enabled_character()
    await _seed_web_conversation(harness, character.id)

    decider = _CapturingDecider()
    dispatcher = _build_dispatcher(
        harness,
        decider=decider,
        dialogue_summarizer=_RecordingSummarizer(output="你剛陪對方處理心情低落"),
    )

    await dispatcher.evaluate(
        character_id=character.id, trigger=ProactiveTrigger.TICK,
    )

    assert decider.last_context is not None
    material = decider.last_context.recent_dialogue_summary
    assert "最近幾則對話原文" in material
    # Raw text, not a paraphrase, and carrying its own anchor.
    assert "使用者：今天有點悶" in material
    assert "剛剛]" in material
    # Seeded seconds ago — nothing in this block may read as another day.
    for word in ("昨天", "前天", "上禮拜"):
        assert word not in material


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
