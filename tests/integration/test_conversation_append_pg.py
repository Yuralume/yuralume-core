"""PostgreSQL integration for the conversation CAS append (LH2, DR-LH0-004).

Drives the REAL ``SAConversationRepository.append_messages`` against Postgres so
the concurrency guarantee the in-memory twin cannot prove holds under true
overlapping transactions on separate asyncpg connections: two turns racing an
append to the SAME conversation at the SAME expected position → exactly one
``ok`` winner, the other a non-writing ``conflict`` (serialised on the
``SELECT ... FOR UPDATE`` row lock).

Requires Docker (testcontainers); skipped automatically when unavailable.
"""

from __future__ import annotations

import asyncio

import pytest

from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageRole,
)
from kokoro_link.infrastructure.persistence.sa_conversation_repository import (
    SAConversationRepository,
)

pytestmark = pytest.mark.asyncio


async def _seed(repo) -> str:
    conv = Conversation.start(character_id="char-1", source="line")
    await repo.save(conv)
    return conv.id


def _msg(role: MessageRole, text: str) -> Message:
    return Message(role=role, content=text)


async def test_concurrent_append_same_position_one_winner(session_factory) -> None:
    repo = SAConversationRepository(session_factory)
    conv_id = await _seed(repo)
    barrier = asyncio.Barrier(2)

    async def _append(text: str):
        await barrier.wait()
        return await repo.append_messages(
            conv_id,
            expected_next_position=0,
            messages=[_msg(MessageRole.USER, text)],
        )

    first, second = await asyncio.gather(_append("A"), _append("B"))
    results = [first, second]
    winners = [r for r in results if r.ok]
    losers = [r for r in results if r.conflict]

    # Exactly one append committed; the other saw the advanced tail.
    assert len(winners) == 1
    assert len(losers) == 1
    assert winners[0].positions == (0,)
    assert losers[0].ok is False
    assert losers[0].positions == ()

    # The conversation holds exactly the one winning row — no lost update, no
    # double write.
    stored = await repo.get(conv_id)
    assert len(stored.messages) == 1
    assert stored.messages[0].content in {"A", "B"}


async def test_serial_append_after_conflict_retargets_next_position(
    session_factory,
) -> None:
    repo = SAConversationRepository(session_factory)
    conv_id = await _seed(repo)

    ok = await repo.append_messages(
        conv_id,
        expected_next_position=0,
        messages=[_msg(MessageRole.USER, "hi")],
    )
    assert ok.ok is True

    # A stale writer still aiming at position 0 is rejected without a write.
    stale = await repo.append_messages(
        conv_id,
        expected_next_position=0,
        messages=[_msg(MessageRole.ASSISTANT, "stale")],
    )
    assert stale.conflict is True

    # Retargeting at the fresh tail succeeds and lands contiguously.
    retried = await repo.append_messages(
        conv_id,
        expected_next_position=1,
        messages=[_msg(MessageRole.ASSISTANT, "fresh")],
    )
    assert retried.ok is True
    assert retried.positions == (1,)

    stored = await repo.get(conv_id)
    assert [m.content for m in stored.messages] == ["hi", "fresh"]


async def test_b3_stale_save_does_not_clobber_concurrent_append(
    session_factory,
) -> None:
    """B3: a whole-conversation ``save()`` replaying a stale snapshot must merge
    (not drop) rows a concurrent CAS ``append_messages`` added after the load."""
    repo = SAConversationRepository(session_factory)
    conv_id = await _seed(repo)
    loaded = await repo.get(conv_id)  # snapshot at the stored version, 0 messages

    ok = await repo.append_messages(
        conv_id,
        expected_next_position=0,
        messages=[_msg(MessageRole.ASSISTANT, "concurrent")],
    )
    assert ok.ok is True

    # The stale save (its snapshot has no messages) must not clobber the append.
    await repo.save(loaded)

    stored = await repo.get(conv_id)
    assert [m.content for m in stored.messages] == ["concurrent"]


async def test_b1_idempotent_user_append_adopts_existing_row(
    session_factory,
) -> None:
    """B1: a keyed re-append (a takeover re-driving after a crash between the
    durable append and the receipt record) adopts the original row rather than
    writing a duplicate."""
    repo = SAConversationRepository(session_factory)
    conv_id = await _seed(repo)

    first = await repo.append_messages(
        conv_id,
        expected_next_position=0,
        messages=[_msg(MessageRole.USER, "hi")],
        idempotency_key="turn-1:user",
    )
    assert first.ok is True

    # Same key, even at a different expected position — adopts the original row.
    second = await repo.append_messages(
        conv_id,
        expected_next_position=1,
        messages=[_msg(MessageRole.USER, "hi again")],
        idempotency_key="turn-1:user",
    )
    assert second.ok is True
    assert second.row_ids == first.row_ids
    assert second.positions == first.positions

    stored = await repo.get(conv_id)
    assert [m.content for m in stored.messages] == ["hi"]
