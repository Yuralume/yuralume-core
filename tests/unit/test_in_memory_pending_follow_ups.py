"""Unit tests for the in-memory ``PendingFollowUpRepositoryPort``."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpMessage,
    PendingFollowUpStatus,
)
from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
    InMemoryPendingFollowUpRepository,
)


def _now() -> datetime:
    return datetime(2026, 5, 16, 14, 0, tzinfo=timezone.utc)


def _row(
    *,
    character_id: str = "char-1",
    conversation_id: str = "conv-1",
    scheduled_for_offset_min: int = 30,
    status: PendingFollowUpStatus = PendingFollowUpStatus.QUEUED,
) -> PendingFollowUp:
    row = PendingFollowUp.new(
        character_id=character_id,
        conversation_id=conversation_id,
        first_message=PendingFollowUpMessage.new(
            content="hi", queued_at=_now(),
        ),
        brief_reply="先回",
        defer_reason="會議中",
        scheduled_for=_now() + timedelta(minutes=scheduled_for_offset_min),
        activity_id="act-1",
        now=_now(),
    )
    if status == PendingFollowUpStatus.RESOLVING:
        return row.marked_resolving()
    if status == PendingFollowUpStatus.RESOLVED:
        return row.marked_resolved(message_text="done")
    if status == PendingFollowUpStatus.CANCELLED:
        return row.cancelled()
    return row


@pytest.mark.asyncio
async def test_add_and_get() -> None:
    repo = InMemoryPendingFollowUpRepository()
    row = _row()
    await repo.add(row)
    assert await repo.get(row.id) == row
    assert await repo.get("missing") is None


@pytest.mark.asyncio
async def test_save_upserts() -> None:
    repo = InMemoryPendingFollowUpRepository()
    row = _row()
    await repo.add(row)
    appended = row.appended(
        PendingFollowUpMessage.new(content="再問一個", queued_at=_now()),
    )
    await repo.save(appended)
    fetched = await repo.get(row.id)
    assert fetched is not None
    assert len(fetched.messages) == 2


@pytest.mark.asyncio
async def test_find_open_for_conversation_excludes_resolved() -> None:
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_row(status=PendingFollowUpStatus.RESOLVED))
    queued = _row(scheduled_for_offset_min=60)
    await repo.add(queued)
    found = await repo.find_open_for_conversation("conv-1")
    assert found is not None
    assert found.id == queued.id


@pytest.mark.asyncio
async def test_find_open_picks_latest_queued_at() -> None:
    repo = InMemoryPendingFollowUpRepository()
    older = _row(scheduled_for_offset_min=30)
    await repo.add(older)
    # Hand-craft a newer row to bypass uuid randomness in queued_at
    newer = older.appended(
        PendingFollowUpMessage.new(content="新的", queued_at=_now() + timedelta(minutes=1)),
        now=_now() + timedelta(minutes=1),
    )
    await repo.save(newer)
    found = await repo.find_open_for_conversation("conv-1")
    assert found is not None
    assert len(found.messages) == 2


@pytest.mark.asyncio
async def test_list_due_returns_only_queued_past_scheduled() -> None:
    repo = InMemoryPendingFollowUpRepository()
    due = _row(scheduled_for_offset_min=-10)
    not_due = _row(conversation_id="conv-2", scheduled_for_offset_min=30)
    resolving = _row(
        conversation_id="conv-3", scheduled_for_offset_min=-5,
        status=PendingFollowUpStatus.RESOLVING,
    )
    await repo.add(due)
    await repo.add(not_due)
    await repo.add(resolving)
    rows = await repo.list_due(now=_now())
    ids = {r.id for r in rows}
    assert due.id in ids
    assert not_due.id not in ids
    assert resolving.id not in ids


@pytest.mark.asyncio
async def test_delete_for_character_cascade() -> None:
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_row(character_id="char-1", conversation_id="conv-a"))
    await repo.add(_row(character_id="char-1", conversation_id="conv-b"))
    await repo.add(_row(character_id="char-2", conversation_id="conv-c"))
    removed = await repo.delete_for_character("char-1")
    assert removed == 2
    assert await repo.list_open_for_character("char-1") == []
    assert len(await repo.list_open_for_character("char-2")) == 1


@pytest.mark.asyncio
async def test_delete_for_conversation_cascade() -> None:
    repo = InMemoryPendingFollowUpRepository()
    await repo.add(_row(conversation_id="conv-x"))
    await repo.add(_row(conversation_id="conv-y"))
    removed = await repo.delete_for_conversation("conv-x")
    assert removed == 1
    assert await repo.find_open_for_conversation("conv-x") is None
    assert await repo.find_open_for_conversation("conv-y") is not None
