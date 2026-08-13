"""The proactive dispatcher records which world event it used.

A claimed inbox row is invisible to the chat peek forever after, so the
moment a push about a news item goes out, chat loses the material behind
it. The mention is the other half of that claim: it is written when — and
only when — something actually reached the player, and chat reads it back
so a follow-up question has an answer.

Reuses the in-memory dispatcher harness shape from the other dispatcher
tests.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.event_seed_dispenser import (
    EventSeedDispenser,
)
from kokoro_link.application.services.proactive_dispatcher import (
    ProactiveDispatcher,
)
from kokoro_link.contracts.proactive import (
    GateVerdict,
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
    ProactiveGatePort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_event_inbox import (
    CharacterEventInboxItem,
)
from kokoro_link.domain.entities.world_event import WorldEvent
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.repositories.in_memory_channel_bindings import (
    InMemoryChannelBindingRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_character_event_inbox import (  # noqa: E501
    InMemoryCharacterEventInboxRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_character_event_mentions import (  # noqa: E501
    InMemoryCharacterEventMentionRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_messaging_accounts import (
    InMemoryMessagingAccountRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_world_events import (
    InMemoryWorldEventRepository,
)

_NOW = datetime(2026, 8, 10, 4, 0, tzinfo=timezone.utc)
_EVENT_ID = "we-ramen"


class _AlwaysPassGate(ProactiveGatePort):
    async def check(self, **_):  # noqa: ANN003, ANN401
        return GateVerdict(passed=True, reason="ok")


class _SendingDecider(ProactiveDeciderPort):
    def __init__(self) -> None:
        self.seen_seed_title = ""

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.seen_seed_title = context.world_event_seed_title
        return ProactiveDecision(
            should_send=True, reason="ok", message="剛剛看到板上在吵那家拉麵店",
        )


class _SilentDecider(ProactiveDeciderPort):
    async def decide(self, _: ProactiveContext) -> ProactiveDecision:
        return ProactiveDecision(
            should_send=False, reason="沒什麼好說", message=None,
        )


class _ExplodingMentions(InMemoryCharacterEventMentionRepository):
    async def record(self, mention) -> None:  # noqa: ANN001
        raise RuntimeError("mention store down")


async def _character(
    repo: InMemoryCharacterRepository, *, world_awareness: bool = True,
) -> Character:
    character = Character.create(
        name="Mio",
        summary="咖啡店打工的大學生",
        personality=["溫柔"],
        interests=["拉麵"],
        speaking_style="輕柔",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=60, fatigue=20, trust=60, energy=80,
            last_active_at=_NOW - timedelta(hours=1),
        ),
        proactive_enabled=True,
        proactive_daily_limit=5,
        accepts_web_proactive=True,
        world_awareness_enabled=world_awareness,
    )
    await repo.save(character)
    return character


async def _seeded_dispenser(
    character_id: str,
) -> tuple[EventSeedDispenser, InMemoryCharacterEventInboxRepository]:
    events = InMemoryWorldEventRepository()
    await events.upsert(
        WorldEvent(
            id=_EVENT_ID,
            source="PTT 熱門看板",
            title="板上在吵新開的拉麵店",
            summary="排隊三小時，湯頭評價兩極。",
            url="https://www.ptt.cc/bbs/Food/M.1.A.1.html",
            published_at=_NOW - timedelta(hours=5),
            fetched_at=_NOW - timedelta(hours=4),
        ),
    )
    inbox = InMemoryCharacterEventInboxRepository()
    await inbox.add_many([
        CharacterEventInboxItem.create(
            character_id=character_id,
            world_event_id=_EVENT_ID,
            similarity=0.8,
            created_at=_NOW - timedelta(hours=3),
        ),
    ])
    return (
        EventSeedDispenser(
            inbox_repository=inbox, world_event_repository=events,
        ),
        inbox,
    )


def _dispatcher(
    *,
    character_repo: InMemoryCharacterRepository,
    decider: ProactiveDeciderPort,
    dispenser: EventSeedDispenser | None,
    mentions,  # noqa: ANN001
) -> ProactiveDispatcher:
    return ProactiveDispatcher(
        character_repository=character_repo,
        conversation_repository=InMemoryConversationRepository(),
        account_repository=InMemoryMessagingAccountRepository(),
        binding_repository=InMemoryChannelBindingRepository(),
        attempt_repository=InMemoryProactiveAttemptRepository(),
        gate=_AlwaysPassGate(),
        decider=decider,
        adapters={},
        event_seed_dispenser=dispenser,
        event_mention_repository=mentions,
    )


@pytest.mark.asyncio
async def test_sent_push_records_the_event_it_used() -> None:
    characters = InMemoryCharacterRepository()
    character = await _character(characters)
    dispenser, _ = await _seeded_dispenser(character.id)
    mentions = InMemoryCharacterEventMentionRepository()
    decider = _SendingDecider()
    dispatcher = _dispatcher(
        character_repo=characters, decider=decider,
        dispenser=dispenser, mentions=mentions,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=_NOW,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    assert decider.seen_seed_title == "板上在吵新開的拉麵店"
    recorded = await mentions.list_recent(character.id)
    assert len(recorded) == 1
    assert recorded[0].world_event_id == _EVENT_ID
    assert recorded[0].surface == "proactive_message"
    assert recorded[0].mentioned_at == _NOW


@pytest.mark.asyncio
async def test_nothing_sent_records_nothing_and_frees_the_seed() -> None:
    characters = InMemoryCharacterRepository()
    character = await _character(characters)
    dispenser, inbox = await _seeded_dispenser(character.id)
    mentions = InMemoryCharacterEventMentionRepository()
    dispatcher = _dispatcher(
        character_repo=characters, decider=_SilentDecider(),
        dispenser=dispenser, mentions=mentions,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=_NOW,
    )

    assert attempt.outcome == ProactiveOutcome.DECIDER_SKIPPED
    assert await mentions.list_recent(character.id) == []
    # The seed went back to the pool rather than being burned.
    unclaimed = await inbox.list_for_character(
        character.id, unclaimed_only=True,
    )
    assert len(unclaimed) == 1


@pytest.mark.asyncio
async def test_no_seed_means_no_mention() -> None:
    characters = InMemoryCharacterRepository()
    character = await _character(characters, world_awareness=False)
    dispenser, _ = await _seeded_dispenser(character.id)
    mentions = InMemoryCharacterEventMentionRepository()
    dispatcher = _dispatcher(
        character_repo=characters, decider=_SendingDecider(),
        dispenser=dispenser, mentions=mentions,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=_NOW,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
    assert await mentions.list_recent(character.id) == []


@pytest.mark.asyncio
async def test_unwired_mention_store_does_not_break_the_push() -> None:
    characters = InMemoryCharacterRepository()
    character = await _character(characters)
    dispenser, _ = await _seeded_dispenser(character.id)
    dispatcher = _dispatcher(
        character_repo=characters, decider=_SendingDecider(),
        dispenser=dispenser, mentions=None,
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=_NOW,
    )

    assert attempt.outcome == ProactiveOutcome.SENT


@pytest.mark.asyncio
async def test_mention_write_failure_never_loses_the_sent_outcome() -> None:
    # The message has already reached the player at this point; a
    # bookkeeping failure must not turn a real send into an error row.
    characters = InMemoryCharacterRepository()
    character = await _character(characters)
    dispenser, _ = await _seeded_dispenser(character.id)
    dispatcher = _dispatcher(
        character_repo=characters, decider=_SendingDecider(),
        dispenser=dispenser, mentions=_ExplodingMentions(),
    )

    attempt = await dispatcher.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=_NOW,
    )

    assert attempt.outcome == ProactiveOutcome.SENT
