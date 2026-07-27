"""CAS-append (``append_messages``) contract — in-memory + SA (SQLite) twins.

Both adapters MUST share one semantic (DR-LH0-004): a position-checked tail
append that never clears the existing rows, so a stale ``expected_next_position``
is rejected without a write and consecutive appends land at consecutive
positions. The true overlapping-transaction race is proven against Postgres in
``tests/integration/test_conversation_append_pg.py``.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageRole,
)
from kokoro_link.infrastructure.persistence.engine import build_session_factory
from kokoro_link.infrastructure.persistence.models import (
    ConversationRow,
    MessageRow,
)
from kokoro_link.infrastructure.persistence.sa_conversation_repository import (
    SAConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)

pytestmark = pytest.mark.asyncio


def _user(text: str) -> Message:
    return Message(role=MessageRole.USER, content=text)


def _assistant(text: str) -> Message:
    return Message(role=MessageRole.ASSISTANT, content=text)


# --------------------------------------------------------------------------- #
# SQLite SA fixture — only the conversation / message tables (no pgvector).
# --------------------------------------------------------------------------- #
@pytest_asyncio.fixture
async def sa_repo():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: ConversationRow.__table__.create(sync_conn),
        )
        await conn.run_sync(
            lambda sync_conn: MessageRow.__table__.create(sync_conn),
        )
    try:
        yield SAConversationRepository(build_session_factory(engine))
    finally:
        await engine.dispose()


async def _seed_conversation(repo) -> str:
    conv = Conversation.start(character_id="char-1", source="line")
    await repo.save(conv)
    return conv.id


# --------------------------------------------------------------------------- #
# In-memory twin.
# --------------------------------------------------------------------------- #
async def test_in_memory_normal_append_from_empty() -> None:
    repo = InMemoryConversationRepository()
    conv_id = await _seed_conversation(repo)

    result = await repo.append_messages(
        conv_id, expected_next_position=0, messages=[_user("hi"), _assistant("yo")],
    )

    assert result.ok is True
    assert result.conflict is False
    assert result.positions == (0, 1)
    assert len(result.row_ids) == 2
    stored = await repo.get(conv_id)
    assert [m.content for m in stored.messages] == ["hi", "yo"]


async def test_in_memory_wrong_expected_position_is_conflict_no_write() -> None:
    repo = InMemoryConversationRepository()
    conv_id = await _seed_conversation(repo)
    await repo.append_messages(
        conv_id, expected_next_position=0, messages=[_user("hi")],
    )

    result = await repo.append_messages(
        conv_id, expected_next_position=0, messages=[_assistant("stale")],
    )

    assert result.ok is False
    assert result.conflict is True
    stored = await repo.get(conv_id)
    assert [m.content for m in stored.messages] == ["hi"]  # nothing written


async def test_in_memory_consecutive_appends_are_contiguous() -> None:
    repo = InMemoryConversationRepository()
    conv_id = await _seed_conversation(repo)

    r0 = await repo.append_messages(
        conv_id, expected_next_position=0, messages=[_user("a")],
    )
    r1 = await repo.append_messages(
        conv_id, expected_next_position=1, messages=[_assistant("b")],
    )
    r2 = await repo.append_messages(
        conv_id, expected_next_position=2, messages=[_user("c")],
    )

    assert (r0.positions, r1.positions, r2.positions) == ((0,), (1,), (2,))
    stored = await repo.get(conv_id)
    assert [m.content for m in stored.messages] == ["a", "b", "c"]


# --------------------------------------------------------------------------- #
# SA (SQLite) twin — same assertions.
# --------------------------------------------------------------------------- #
async def test_sa_normal_append_from_empty(sa_repo) -> None:
    conv_id = await _seed_conversation(sa_repo)

    result = await sa_repo.append_messages(
        conv_id, expected_next_position=0, messages=[_user("hi"), _assistant("yo")],
    )

    assert result.ok is True
    assert result.conflict is False
    assert result.positions == (0, 1)
    assert all(isinstance(rid, int) for rid in result.row_ids)
    stored = await sa_repo.get(conv_id)
    assert [m.content for m in stored.messages] == ["hi", "yo"]


async def test_sa_wrong_expected_position_is_conflict_no_write(sa_repo) -> None:
    conv_id = await _seed_conversation(sa_repo)
    await sa_repo.append_messages(
        conv_id, expected_next_position=0, messages=[_user("hi")],
    )

    result = await sa_repo.append_messages(
        conv_id, expected_next_position=0, messages=[_assistant("stale")],
    )

    assert result.ok is False
    assert result.conflict is True
    stored = await sa_repo.get(conv_id)
    assert [m.content for m in stored.messages] == ["hi"]


async def test_sa_consecutive_appends_are_contiguous(sa_repo) -> None:
    conv_id = await _seed_conversation(sa_repo)

    await sa_repo.append_messages(
        conv_id, expected_next_position=0, messages=[_user("a")],
    )
    await sa_repo.append_messages(
        conv_id, expected_next_position=1, messages=[_assistant("b")],
    )
    r2 = await sa_repo.append_messages(
        conv_id, expected_next_position=2, messages=[_user("c")],
    )

    assert r2.ok is True
    assert r2.positions == (2,)
    stored = await sa_repo.get(conv_id)
    assert [m.content for m in stored.messages] == ["a", "b", "c"]


async def test_sa_append_to_absent_conversation_is_conflict(sa_repo) -> None:
    result = await sa_repo.append_messages(
        "ghost", expected_next_position=0, messages=[_user("hi")],
    )
    assert result.ok is False
    assert result.conflict is True
