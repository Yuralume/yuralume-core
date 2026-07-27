"""sol review-fix locks for the external-chat turn service (H5 / M9 / M10)."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from kokoro_link.application.dto.external_chat import ExternalChatAttachmentInput
from kokoro_link.application.services.external_chat_attachment_service import (
    inbound_object_key_prefix,
)
from kokoro_link.contracts.external_chat_turn import (
    ExternalChatTurnReceipt,
    ExternalChatTurnState,
)
from kokoro_link.contracts.object_storage import (
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStorageUnavailableError,
)
from kokoro_link.domain.entities.conversation import Conversation

from tests.unit.external_chat_turn_harness import (
    CHARACTER_ID,
    OPERATOR_ID,
    FakeObjectStorage,
    build_harness,
    make_command,
)

pytestmark = pytest.mark.asyncio


# --- H5: attachment ownership + declared-metadata verification --------------


def _seed_attachment(
    *, key: str, sha256: str, size: int, media_type: str,
) -> ObjectMetadata:
    return ObjectMetadata(
        object_key=key,
        url=f"/v1/public/{key}",
        content_type=media_type,
        size_bytes=size,
        sha256=sha256,
    )


async def test_h5_cross_tenant_attachment_key_is_opaque_404() -> None:
    # A key under a DIFFERENT operator's inbound prefix must never be fetched.
    foreign_key = "users/cloud-someone-else/messaging-inbound/deadbeef.png"
    storage = FakeObjectStorage(
        stats={
            foreign_key: _seed_attachment(
                key=foreign_key, sha256="a" * 64, size=10, media_type="image/png",
            ),
        },
    )
    harness = await build_harness(storage=storage)
    command = make_command(
        attachments=(
            ExternalChatAttachmentInput(
                object_ref=foreign_key,
                media_type="image/png",
                size_bytes=10,
                sha256="a" * 64,
            ),
        ),
    )

    result = await harness.turn_service.execute(command)

    assert result.status_code == 404
    assert harness.model.generate_calls == 0  # LLM never ran on a foreign object


async def test_h5_declared_metadata_mismatch_is_409() -> None:
    key = f"{inbound_object_key_prefix(OPERATOR_ID)}abc123.png"
    storage = FakeObjectStorage(
        stats={
            key: _seed_attachment(
                key=key, sha256="b" * 64, size=100, media_type="image/png",
            ),
        },
    )
    harness = await build_harness(storage=storage)
    # Declared sha256 disagrees with the stored object.
    command = make_command(
        attachments=(
            ExternalChatAttachmentInput(
                object_ref=key,
                media_type="image/png",
                size_bytes=100,
                sha256="c" * 64,
            ),
        ),
    )

    result = await harness.turn_service.execute(command)

    assert result.status_code == 409
    body = json.loads(result.body_json)
    assert body["error"]["code"] == "attachment_mismatch"
    assert harness.model.generate_calls == 0


async def test_h5_owned_and_matching_attachment_is_accepted() -> None:
    key = f"{inbound_object_key_prefix(OPERATOR_ID)}ok.png"
    storage = FakeObjectStorage(
        stats={
            key: _seed_attachment(
                key=key, sha256="d" * 64, size=42, media_type="image/png",
            ),
        },
    )
    harness = await build_harness(storage=storage)
    command = make_command(
        attachments=(
            ExternalChatAttachmentInput(
                object_ref=key, media_type="image/png", size_bytes=42,
                sha256="d" * 64,
            ),
        ),
    )

    result = await harness.turn_service.execute(command)

    assert result.status_code == 200
    assert harness.model.generate_calls == 1


# --- H5 (round 2): a None stored digest is re-hashed, not skipped; a transient
# storage failure is a retryable 503, never a terminal 404 --------------------


class _DigestlessStorage(FakeObjectStorage):
    """``stat`` returns metadata with NO sha256 (the store never computed one);
    ``get_bytes`` serves the real bytes so the service can re-hash them."""

    def __init__(self, *, key: str, data: bytes, media_type: str) -> None:
        super().__init__(
            stats={
                key: ObjectMetadata(
                    object_key=key,
                    url=f"/v1/public/{key}",
                    content_type=media_type,
                    size_bytes=len(data),
                    sha256=None,
                ),
            },
        )
        self._bytes = {key: data}
        self.get_bytes_calls = 0

    async def get_bytes(self, *, object_key: str) -> bytes:
        self.get_bytes_calls += 1
        return self._bytes[object_key]


class _FlakyStorage(FakeObjectStorage):
    """``stat`` raises the given storage error, simulating a not-found vs a
    transient backend failure."""

    def __init__(self, *, error: Exception) -> None:
        super().__init__(stats={})
        self._error = error

    async def stat(self, *, object_key: str):  # noqa: ANN001
        raise self._error


def _hexdigest(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


async def test_h5_none_stored_digest_is_recomputed_and_matched() -> None:
    data = b"\x89PNG\r\n\x1a\nreal-bytes"
    key = f"{inbound_object_key_prefix(OPERATOR_ID)}nodigest.png"
    storage = _DigestlessStorage(key=key, data=data, media_type="image/png")
    harness = await build_harness(storage=storage)
    command = make_command(
        attachments=(
            ExternalChatAttachmentInput(
                object_ref=key, media_type="image/png",
                size_bytes=len(data), sha256=_hexdigest(data),
            ),
        ),
    )

    result = await harness.turn_service.execute(command)

    assert result.status_code == 200
    # Verification was NOT skipped — the bytes were fetched and re-hashed.
    assert storage.get_bytes_calls == 1
    assert harness.model.generate_calls == 1


async def test_h5_none_stored_digest_recompute_mismatch_is_409() -> None:
    data = b"\x89PNG\r\n\x1a\nreal-bytes"
    key = f"{inbound_object_key_prefix(OPERATOR_ID)}nodigest.png"
    storage = _DigestlessStorage(key=key, data=data, media_type="image/png")
    harness = await build_harness(storage=storage)
    # Declared digest disagrees with the actual bytes → 409, LLM never runs.
    command = make_command(
        attachments=(
            ExternalChatAttachmentInput(
                object_ref=key, media_type="image/png",
                size_bytes=len(data), sha256="f" * 64,
            ),
        ),
    )

    result = await harness.turn_service.execute(command)

    assert result.status_code == 409
    assert json.loads(result.body_json)["error"]["code"] == "attachment_mismatch"
    assert storage.get_bytes_calls == 1
    assert harness.model.generate_calls == 0


async def test_h5_storage_not_found_is_terminal_404() -> None:
    key = f"{inbound_object_key_prefix(OPERATOR_ID)}gone.png"
    storage = _FlakyStorage(error=ObjectNotFoundError("gone"))
    harness = await build_harness(storage=storage)
    command = make_command(
        attachments=(
            ExternalChatAttachmentInput(
                object_ref=key, media_type="image/png", size_bytes=10,
                sha256="a" * 64,
            ),
        ),
    )

    result = await harness.turn_service.execute(command)

    assert result.status_code == 404
    assert json.loads(result.body_json)["error"]["retryable"] is False


async def test_h5_transient_storage_error_is_retryable_503_not_terminal() -> None:
    key = f"{inbound_object_key_prefix(OPERATOR_ID)}flaky.png"
    storage = _FlakyStorage(
        error=ObjectStorageUnavailableError("backend down"),
    )
    harness = await build_harness(storage=storage)
    command = make_command(
        attachments=(
            ExternalChatAttachmentInput(
                object_ref=key, media_type="image/png", size_bytes=10,
                sha256="a" * 64,
            ),
        ),
    )

    result = await harness.turn_service.execute(command)

    # A backend outage must NOT burn the recoverable turn into a terminal 404.
    assert result.status_code == 503
    assert json.loads(result.body_json)["error"]["retryable"] is True
    assert harness.model.generate_calls == 0


# --- M9: inbound reuses the proactively-created source="line" thread --------


async def test_m9_inbound_reuses_proactive_line_conversation() -> None:
    conv_repo = None
    from kokoro_link.infrastructure.repositories.in_memory_conversations import (
        InMemoryConversationRepository,
    )
    conv_repo = InMemoryConversationRepository()
    # A proactive push already created the character's source="line" thread.
    proactive_conv = Conversation.start(character_id=CHARACTER_ID, source="line")
    await conv_repo.save(proactive_conv)

    harness = await build_harness(conversation_repo=conv_repo)
    # Inbound turn carries NO conversation pointer.
    result = await harness.turn_service.execute(make_command())

    assert result.status_code == 200
    body = json.loads(result.body_json)
    # The turn landed on the SAME line conversation, not a forked new one.
    assert body["conversation_id"] == proactive_conv.id


async def test_m9_concurrent_double_create_converges_on_one_conversation() -> None:
    """A genuine inbound-vs-proactive race that each created a line thread
    (the double-create window) must converge: the shared resolver returns ONE
    deterministic winner for every caller so both directions land on it."""
    from kokoro_link.application.services.external_chat.line_conversation import (
        resolve_or_create_line_conversation,
    )
    from kokoro_link.infrastructure.repositories.in_memory_conversations import (
        InMemoryConversationRepository,
    )

    repo = InMemoryConversationRepository()
    # Two empty line threads already exist — the outcome of a create race.
    forked_a = Conversation.start(character_id=CHARACTER_ID, source="line")
    forked_b = Conversation.start(character_id=CHARACTER_ID, source="line")
    await repo.save(forked_a)
    await repo.save(forked_b)

    # Every caller (inbound resolver, proactive recorder) routes through the one
    # shared resolver and must agree on the same winner.
    first = await resolve_or_create_line_conversation(
        repo, character_id=CHARACTER_ID,
    )
    second = await resolve_or_create_line_conversation(
        repo, character_id=CHARACTER_ID,
    )
    assert first.id == second.id
    assert first.id in {forked_a.id, forked_b.id}


# --- M10: a fenced-out terminal write re-reads the real outcome -------------


class _FencedOutReceipts:
    """Delegates to the real receipt repo but forces ``mark_failed`` to report a
    fenced-out write (False) and resolves ``get`` to a COMPLETED receipt driven
    by a concurrent owner, so the service must replay THAT outcome (M10)."""

    _SNAPSHOT = json.dumps({"request_id": "x", "turn_id": "t", "segments": []})

    def __init__(self, inner) -> None:
        self._inner = inner

    async def mark_failed(self, **_kwargs) -> bool:
        return False

    async def get(self, turn_id: str) -> ExternalChatTurnReceipt:
        from dataclasses import replace
        base = await self._inner.get(turn_id)
        assert base is not None
        return replace(
            base,
            state=ExternalChatTurnState.COMPLETED,
            response_snapshot_json=self._SNAPSHOT,
        )

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def test_m10_fenced_terminal_replays_actual_completed_outcome() -> None:
    harness = await build_harness()
    harness.turn_service._receipts = _FencedOutReceipts(harness.receipt_repo)
    # Force the terminal branch: an account that resolves no operator.
    command = make_command(account_id="ghost-account")

    result = await harness.turn_service.execute(command)

    # Instead of asserting our stale 404, the service replayed the concurrent
    # owner's COMPLETED snapshot.
    assert result.status_code == 200
    assert result.body_json == _FencedOutReceipts._SNAPSHOT
