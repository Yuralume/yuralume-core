"""ChatService rebuilds context for world events it already used.

Once a surface claims an inbox row the chat peek can never return it, so
a proactive push about a news item used to erase its own material. These
tests pin the loader that puts it back: newest first, link included,
bounded by age, and fail-soft against every dependency (including a
mention pointing at an event the GC already deleted).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_event_mention import (
    CharacterEventMention,
)
from kokoro_link.domain.entities.world_event import WorldEvent
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_character_event_mentions import (  # noqa: E501
    InMemoryCharacterEventMentionRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_world_events import (
    InMemoryWorldEventRepository,
)

UTC = timezone.utc
_NOW = datetime(2026, 8, 10, 12, 0, tzinfo=UTC)


def _character(*, world_awareness: bool = True) -> Character:
    return Character.create(
        name="Mio", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        world_awareness_enabled=world_awareness,
    )


def _event(event_id: str, *, title: str = "板上在吵新開的拉麵店") -> WorldEvent:
    return WorldEvent(
        id=event_id,
        source="PTT 熱門看板",
        title=title,
        summary="排隊三小時，湯頭評價兩極。",
        url=f"https://www.ptt.cc/bbs/Food/{event_id}.html",
        published_at=_NOW - timedelta(hours=8),
        fetched_at=_NOW - timedelta(hours=7),
    )


def _service(*, mentions=None, events=None) -> ChatService:  # noqa: ANN001
    service = ChatService.__new__(ChatService)
    service._event_mentions = mentions  # noqa: SLF001
    service._world_events = events  # noqa: SLF001
    return service


async def _rig(
    character: Character, *, mentioned_at: datetime = _NOW - timedelta(hours=2),
):  # noqa: ANN202
    events = InMemoryWorldEventRepository()
    await events.upsert(_event("we-1"))
    mentions = InMemoryCharacterEventMentionRepository()
    await mentions.record(
        CharacterEventMention.create(
            character_id=character.id,
            world_event_id="we-1",
            surface="proactive_message",
            mentioned_at=mentioned_at,
        ),
    )
    return _service(mentions=mentions, events=events)


@pytest.mark.asyncio
async def test_recalls_a_used_event_with_its_link() -> None:
    character = _character()
    service = await _rig(character)

    lines = await service._load_world_event_recall(character, now=_NOW)  # noqa: SLF001

    assert len(lines) == 1
    assert "板上在吵新開的拉麵店" in lines[0]
    assert "https://www.ptt.cc/bbs/Food/we-1.html" in lines[0]
    assert "2 小時前" in lines[0]


@pytest.mark.asyncio
async def test_newest_first_within_the_limit() -> None:
    character = _character()
    events = InMemoryWorldEventRepository()
    mentions = InMemoryCharacterEventMentionRepository()
    for index in range(5):
        await events.upsert(_event(f"we-{index}", title=f"事件 {index}"))
        await mentions.record(
            CharacterEventMention.create(
                character_id=character.id,
                world_event_id=f"we-{index}",
                surface="proactive_message",
                mentioned_at=_NOW - timedelta(hours=index),
            ),
        )
    service = _service(mentions=mentions, events=events)

    lines = await service._load_world_event_recall(character, now=_NOW)  # noqa: SLF001

    assert len(lines) == 3
    assert "事件 0" in lines[0]
    assert "事件 2" in lines[2]


@pytest.mark.asyncio
async def test_stale_mention_falls_out_of_the_window() -> None:
    character = _character()
    service = await _rig(character, mentioned_at=_NOW - timedelta(days=4))

    assert await service._load_world_event_recall(character, now=_NOW) == ()  # noqa: SLF001


@pytest.mark.asyncio
async def test_world_awareness_off_recalls_nothing() -> None:
    character = _character(world_awareness=False)
    service = await _rig(character)

    assert await service._load_world_event_recall(character, now=_NOW) == ()  # noqa: SLF001


@pytest.mark.asyncio
async def test_unwired_dependencies_recall_nothing() -> None:
    character = _character()
    events = InMemoryWorldEventRepository()
    mentions = InMemoryCharacterEventMentionRepository()

    assert await _service()._load_world_event_recall(character, now=_NOW) == ()  # noqa: SLF001
    assert await _service(mentions=mentions)._load_world_event_recall(  # noqa: SLF001
        character, now=_NOW,
    ) == ()
    assert await _service(events=events)._load_world_event_recall(  # noqa: SLF001
        character, now=_NOW,
    ) == ()


@pytest.mark.asyncio
async def test_event_deleted_by_gc_is_skipped_not_fatal() -> None:
    # PostgreSQL cascades the mention away with the event; self-host
    # SQLite does not, so a dangling pointer is a normal state there.
    character = _character()
    events = InMemoryWorldEventRepository()
    mentions = InMemoryCharacterEventMentionRepository()
    await events.upsert(_event("we-alive"))
    for event_id in ("we-gone", "we-alive"):
        await mentions.record(
            CharacterEventMention.create(
                character_id=character.id,
                world_event_id=event_id,
                surface="proactive_message",
                mentioned_at=_NOW - timedelta(minutes=30),
            ),
        )
    service = _service(mentions=mentions, events=events)

    lines = await service._load_world_event_recall(character, now=_NOW)  # noqa: SLF001

    assert len(lines) == 1
    assert "we-alive" in lines[0]


@pytest.mark.asyncio
async def test_repository_failure_degrades_to_no_block() -> None:
    class _Exploding(InMemoryCharacterEventMentionRepository):
        async def list_recent(self, character_id: str, *, limit: int = 5):  # noqa: ANN201
            raise RuntimeError("store down")

    character = _character()
    service = _service(
        mentions=_Exploding(), events=InMemoryWorldEventRepository(),
    )

    assert await service._load_world_event_recall(character, now=_NOW) == ()  # noqa: SLF001


@pytest.mark.asyncio
async def test_event_fetch_failure_skips_only_that_row() -> None:
    class _HalfBroken(InMemoryWorldEventRepository):
        async def get(self, event_id: str):  # noqa: ANN201
            if event_id == "we-bad":
                raise RuntimeError("pool read failed")
            return await super().get(event_id)

    character = _character()
    events = _HalfBroken()
    await events.upsert(_event("we-ok"))
    mentions = InMemoryCharacterEventMentionRepository()
    for event_id in ("we-bad", "we-ok"):
        await mentions.record(
            CharacterEventMention.create(
                character_id=character.id,
                world_event_id=event_id,
                surface="proactive_message",
                mentioned_at=_NOW - timedelta(minutes=10),
            ),
        )
    service = _service(mentions=mentions, events=events)

    lines = await service._load_world_event_recall(character, now=_NOW)  # noqa: SLF001

    assert len(lines) == 1
    assert "we-ok" in lines[0]


@pytest.mark.asyncio
async def test_dangling_rows_do_not_cost_live_events_their_slot() -> None:
    """The over-fetch: asking for exactly the limit would let three
    GC'd mentions hide three perfectly good ones behind them."""
    character = _character()
    events = InMemoryWorldEventRepository()
    mentions = InMemoryCharacterEventMentionRepository()
    # Newest three point at events the GC already deleted.
    for index in range(3):
        await mentions.record(
            CharacterEventMention.create(
                character_id=character.id,
                world_event_id=f"we-gone-{index}",
                surface="proactive_message",
                mentioned_at=_NOW - timedelta(minutes=index),
            ),
        )
    for index in range(3):
        await events.upsert(_event(f"we-live-{index}", title=f"還在的事件 {index}"))
        await mentions.record(
            CharacterEventMention.create(
                character_id=character.id,
                world_event_id=f"we-live-{index}",
                surface="proactive_message",
                mentioned_at=_NOW - timedelta(hours=1, minutes=index),
            ),
        )
    service = _service(mentions=mentions, events=events)

    lines = await service._load_world_event_recall(character, now=_NOW)  # noqa: SLF001

    assert len(lines) == 3
    assert all("還在的事件" in line for line in lines)
