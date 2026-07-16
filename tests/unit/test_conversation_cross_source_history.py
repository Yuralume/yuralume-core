"""Cross-source message merge guard.

The character is one person across every channel — ``recent_messages_for_character``
must return messages from web + telegram + line interleaved by
``created_at``, not bucket them by source.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageKind,
    MessageRole,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)


def _ts(minutes_ago: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)


@pytest.mark.asyncio
async def test_merges_messages_from_every_source_by_created_at() -> None:
    repo = InMemoryConversationRepository()
    character_id = "char-A"

    web = Conversation.start(character_id=character_id, source="web")
    web = web.append(Message(
        role=MessageRole.USER, content="web-1", created_at=_ts(60),
    ))
    web = web.append(Message(
        role=MessageRole.ASSISTANT, content="web-2", created_at=_ts(58),
    ))
    await repo.save(web)

    tg = Conversation.start(character_id=character_id, source="telegram")
    tg = tg.append(Message(
        role=MessageRole.USER, content="tg-1", created_at=_ts(30),
    ))
    tg = tg.append(Message(
        role=MessageRole.ASSISTANT, content="tg-2", created_at=_ts(25),
    ))
    await repo.save(tg)

    line = Conversation.start(character_id=character_id, source="line")
    line = line.append(Message(
        role=MessageRole.USER, content="line-1", created_at=_ts(45),
    ))
    await repo.save(line)

    merged = await repo.recent_messages_for_character(character_id, limit=10)

    assert [m.content for m in merged] == [
        "web-1", "web-2", "line-1", "tg-1", "tg-2",
    ]


@pytest.mark.asyncio
async def test_limit_returns_tail_only() -> None:
    repo = InMemoryConversationRepository()
    character_id = "char-A"

    web = Conversation.start(character_id=character_id, source="web")
    for i in range(5):
        web = web.append(Message(
            role=MessageRole.USER,
            content=f"m{i}",
            created_at=_ts(100 - i * 10),
        ))
    await repo.save(web)

    merged = await repo.recent_messages_for_character(character_id, limit=2)
    assert [m.content for m in merged] == ["m3", "m4"]


@pytest.mark.asyncio
async def test_exclude_tool_only_drops_those_messages() -> None:
    repo = InMemoryConversationRepository()
    character_id = "char-A"

    web = Conversation.start(character_id=character_id, source="web")
    web = web.append(Message(
        role=MessageRole.USER, content="chat", created_at=_ts(20),
    ))
    web = web.append(Message(
        role=MessageRole.ASSISTANT,
        content="",
        kind=MessageKind.TOOL_ONLY,
        created_at=_ts(15),
    ))
    web = web.append(Message(
        role=MessageRole.ASSISTANT, content="reply", created_at=_ts(10),
    ))
    await repo.save(web)

    merged = await repo.recent_messages_for_character(
        character_id, limit=10, exclude_tool_only=True,
    )
    assert [m.content for m in merged] == ["chat", "reply"]


@pytest.mark.asyncio
async def test_other_character_messages_excluded() -> None:
    repo = InMemoryConversationRepository()

    a_web = Conversation.start(character_id="char-A", source="web")
    a_web = a_web.append(Message(
        role=MessageRole.USER, content="A's", created_at=_ts(10),
    ))
    await repo.save(a_web)

    b_tg = Conversation.start(character_id="char-B", source="telegram")
    b_tg = b_tg.append(Message(
        role=MessageRole.USER, content="B's", created_at=_ts(5),
    ))
    await repo.save(b_tg)

    merged = await repo.recent_messages_for_character("char-A", limit=10)
    assert [m.content for m in merged] == ["A's"]
