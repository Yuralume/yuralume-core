"""sol review-fix locks: H6 (attachment dimension fail-closed), B1 (idempotent
user-turn append under crash/takeover), and B3 (version-gated save merge)."""

from __future__ import annotations

import pytest

from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageRole,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from tests.unit.external_chat_turn_harness import build_harness, make_command
from tests.unit.test_external_chat_attachment_service import (
    _ACCOUNT,
    _CHARACTER_ID,
    _TENANT,
    UnsupportedType,
    _character,
    _service,
)

pytestmark = pytest.mark.asyncio


async def _ingest_bytes(data: bytes, declared_content_type: str):
    service = _service(characters=[_character()])
    return await service.ingest(
        tenant_id=_TENANT,
        account_id=_ACCOUNT,
        character_id=_CHARACTER_ID,
        data=data,
        declared_content_type=declared_content_type,
    )


# --- H6: an image whose dimensions cannot be parsed is rejected -------------


async def test_h6_malformed_webp_unknown_fourcc_is_rejected() -> None:
    # Valid RIFF/WEBP magic (sniff passes) but an unknown codec fourcc, so no
    # dimensions can be recovered — must fail closed, not be accepted on trust.
    body = b"RIFF" + (12).to_bytes(4, "little") + b"WEBP" + b"XXXX" + b"\x00" * 4
    outcome = await _ingest_bytes(body, "image/webp")
    assert isinstance(outcome, UnsupportedType)


async def test_h6_truncated_jpeg_without_sof_is_rejected() -> None:
    # JPEG magic present but no Start-Of-Frame marker → dimensions None → reject.
    body = b"\xff\xd8\xff" + b"\x00" * 24
    outcome = await _ingest_bytes(body, "image/jpeg")
    assert isinstance(outcome, UnsupportedType)


# --- B1: a takeover after a crash between append and record never doubles ----


class _RecordUserTurnCrashesOnce:
    """Delegates to the real receipt repo but makes the FIRST
    ``record_user_turn`` raise — simulating a crash after the durable user
    append but before the receipt records it."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self._raised = False

    async def record_user_turn(self, **kwargs) -> bool:
        if not self._raised:
            self._raised = True
            raise RuntimeError("crash after append, before record")
        return await self._inner.record_user_turn(**kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def test_b1_takeover_does_not_double_append_user_turn() -> None:
    harness = await build_harness()
    harness.turn_service._receipts = _RecordUserTurnCrashesOnce(
        harness.receipt_repo,
    )
    command = make_command()

    # Attempt 1: user turn is durably appended, then the record crashes.
    first = await harness.turn_service.execute(command)
    assert first.status_code == 503  # safe retry

    # Attempt 2 (same request_id): takeover re-drives and completes.
    second = await harness.turn_service.execute(command)
    assert second.status_code == 200

    # Exactly ONE user message survives the crash + takeover.
    convs = [
        c for c in harness.conversation_repo._conversations.values()
    ]
    assert len(convs) == 1
    user_messages = [
        m for m in convs[0].messages if m.role is MessageRole.USER
    ]
    assert len(user_messages) == 1


# --- B3: save() merges a concurrent append only when the version proves it ---


async def test_b3_stale_save_preserves_concurrent_append() -> None:
    repo = InMemoryConversationRepository()
    conv = Conversation.start(character_id="c1", source="line")
    await repo.save(conv)
    loaded = await repo.get(conv.id)  # snapshot at the stored version

    # A concurrent CAS append lands after the snapshot was read.
    await repo.append_messages(
        conv.id,
        expected_next_position=0,
        messages=[Message(role=MessageRole.ASSISTANT, content="concurrent")],
    )

    # The stale whole-conversation save (snapshot has NO messages) must NOT
    # clobber the concurrently-appended row.
    await repo.save(loaded)

    after = await repo.get(conv.id)
    assert [m.content for m in after.messages] == ["concurrent"]


async def test_b3_non_concurrent_truncation_still_shrinks() -> None:
    repo = InMemoryConversationRepository()
    conv = Conversation.start(character_id="c1", source="web")
    await repo.save(conv)
    await repo.append_messages(
        conv.id,
        expected_next_position=0,
        messages=[Message(role=MessageRole.USER, content="a")],
    )
    await repo.append_messages(
        conv.id,
        expected_next_position=1,
        messages=[Message(role=MessageRole.ASSISTANT, content="b")],
    )

    loaded = await repo.get(conv.id)
    # Intentional truncation (undo) from the CURRENT version — must shrink, not
    # merge back the dropped tail.
    truncated = Conversation(
        id=loaded.id,
        character_id=loaded.character_id,
        messages=list(loaded.messages[:1]),
        source=loaded.source,
        version=loaded.version,
    )
    await repo.save(truncated)

    after = await repo.get(conv.id)
    assert [m.content for m in after.messages] == ["a"]


# --- B1b: a whole-conversation save preserves keyed appends' idempotency key --


async def test_b1b_save_preserves_idempotency_key_across_rebuild() -> None:
    repo = InMemoryConversationRepository()
    conv = Conversation.start(character_id="c1", source="line")
    await repo.save(conv)
    # A durable keyed append (as the external-turn / proactive path makes them).
    await repo.append_messages(
        conv.id,
        expected_next_position=0,
        messages=[Message(role=MessageRole.USER, content="u1")],
        idempotency_key="turn-1:user",
    )

    # A whole-conversation replace rebuilds every row — the key must survive it,
    # not be wiped (which would re-open the duplicate-append crash window).
    loaded = await repo.get(conv.id)
    await repo.save(loaded)

    # Re-appending under the same key adopts the existing row (idempotent); no
    # duplicate is written.
    result = await repo.append_messages(
        conv.id,
        expected_next_position=1,
        messages=[Message(role=MessageRole.ASSISTANT, content="dup")],
        idempotency_key="turn-1:user",
    )
    assert result.ok and result.conflict is False
    after = await repo.get(conv.id)
    assert [m.content for m in after.messages] == ["u1"]


# --- B3 (round 2): same-length divergence + undo-truncation authority --------


async def test_b3_same_length_divergence_keeps_concurrent_append() -> None:
    """A stale save whose write length EQUALS the DB length (the writer added
    its own message while a concurrent CAS append added a different one) must
    keep BOTH — not clobber the concurrent append with the writer's message."""
    repo = InMemoryConversationRepository()
    conv = Conversation.start(character_id="c1", source="line")
    await repo.save(conv)
    loaded = await repo.get(conv.id)  # snapshot: 0 messages

    # Concurrent CAS append (keyed, as durable appends are) lands after the read.
    await repo.append_messages(
        conv.id,
        expected_next_position=0,
        messages=[Message(role=MessageRole.ASSISTANT, content="concurrent")],
        idempotency_key="other-turn:assistant",
    )

    # The writer appends its OWN message and saves — same resulting length (1)
    # as the DB, but a different tail.
    web = loaded.append(Message(role=MessageRole.USER, content="web-msg"))
    await repo.save(web)

    after = await repo.get(conv.id)
    contents = [m.content for m in after.messages]
    assert "concurrent" in contents  # the CAS append was NOT clobbered
    assert "web-msg" in contents


async def test_b3_undo_truncation_does_not_resurrect_dropped_tail() -> None:
    """An undo racing a concurrent append must not resurrect the tail it is
    deliberately dropping; ``truncation=True`` makes the shrink authoritative."""
    repo = InMemoryConversationRepository()
    conv = Conversation.start(character_id="c1", source="line")
    await repo.save(conv)
    await repo.append_messages(
        conv.id, expected_next_position=0,
        messages=[Message(role=MessageRole.USER, content="m0")],
        idempotency_key="t:0",
    )
    await repo.append_messages(
        conv.id, expected_next_position=1,
        messages=[Message(role=MessageRole.ASSISTANT, content="m1")],
        idempotency_key="t:1",
    )
    loaded = await repo.get(conv.id)  # [m0, m1], boundary=2

    # A concurrent append races the undo.
    await repo.append_messages(
        conv.id, expected_next_position=2,
        messages=[Message(role=MessageRole.USER, content="m2")],
        idempotency_key="t:2",
    )

    # Undo truncates back to just m0 — authoritative.
    truncated = Conversation(
        id=loaded.id,
        character_id=loaded.character_id,
        messages=list(loaded.messages[:1]),
        source=loaded.source,
        version=loaded.version,
        loaded_message_count=loaded.loaded_message_count,
    )
    await repo.save(truncated, truncation=True)

    after = await repo.get(conv.id)
    contents = [m.content for m in after.messages]
    assert "m1" not in contents  # the dropped tail was NOT resurrected
    assert contents == ["m0"]
