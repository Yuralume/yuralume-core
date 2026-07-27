"""Unit coverage for :class:`ExternalChatTurnService` (LH2, Core-C2).

Drives the real orchestrator over in-memory repos + the counting fake LLM:
happy path (one LLM call, COMPLETED, response schema field-for-field), the
byte-identical completed replay, the 202 in-progress, the 409 hash conflict,
each terminal guard (operator pairing / character-not-in-roster / subscription
denied / conversation-character mismatch) with its stable terminal replay, the
source="line" self-heal, and the runtime-limit → 429 / generic → 503 mappings.
"""

from __future__ import annotations

import json

import pytest

from kokoro_link.application.services.chat_service import (
    ChatRuntimeLimitExceeded,
)
from kokoro_link.application.services.external_chat.canonical_hash import (
    compute_canonical_request_hash,
)
from kokoro_link.application.services.external_chat.turn_support import (
    build_response_snapshot,
)
from kokoro_link.application.services.external_chat_roster_service import (
    RosterCharacterView,
)
from kokoro_link.application.services.external_chat_turn_service import (
    _AUTHENTICATED_CALLER,
    _build_canonical_payload,
)
from kokoro_link.contracts.external_chat_turn import ExternalChatTurnState
from kokoro_link.contracts.object_storage import ObjectMetadata
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageAttachment,
    MessageRole,
)

from tests.unit.external_chat_turn_harness import (
    CHARACTER_ID,
    FakeObjectStorage,
    build_harness,
    make_command,
)

pytestmark = pytest.mark.asyncio


async def _preclaim(harness, command, *, canonical_hash: str | None = None):
    """Seed a live PROCESSING claim for ``command`` (optionally under a
    different hash) so the next ``execute`` observes an existing receipt."""
    payload = _build_canonical_payload(command)
    computed = canonical_hash or compute_canonical_request_hash(payload)
    return await harness.receipt_repo.claim_or_get(
        authenticated_caller=_AUTHENTICATED_CALLER,
        request_id=command.request_id,
        canonical_request_hash=computed,
        canonical_hash_version=1,
        contract_version=command.contract_version,
        tenant_id=command.tenant_id,
        account_id=command.account_id,
        character_id=command.character_id,
        requested_conversation_id=command.conversation_id,
    )


# --- happy path ------------------------------------------------------------ #


async def test_happy_path_full_chain() -> None:
    harness = await build_harness()
    command = make_command()

    result = await harness.turn_service.execute(command)

    assert result.status_code == 200
    assert harness.model.generate_calls == 1
    body = json.loads(result.body_json)
    # Response schema, field for field.
    assert body["request_id"] == command.request_id
    assert body["turn_id"]
    assert body["conversation_id"]
    assert body["attachments"] == []
    assert body["segments"] and all(seg["text"] for seg in body["segments"])
    assert body["character"] == {
        "character_id": CHARACTER_ID,
        "display_name": "Airi",
        "background_frozen": False,
        "avatar_object_ref": None,
        "presentation_version": body["character"]["presentation_version"],
    }
    assert len(body["character"]["presentation_version"]) == 16

    # Receipt walked to COMPLETED; a source="line" conversation was recorded.
    receipt = await harness.receipt_repo.get(body["turn_id"])
    assert receipt.state is ExternalChatTurnState.COMPLETED
    assert receipt.conversation_id == body["conversation_id"]
    conversation = await harness.conversation_repo.get(body["conversation_id"])
    assert conversation.source == "line"
    assert [m.role for m in conversation.messages] == [
        MessageRole.USER, MessageRole.ASSISTANT,
    ]


async def test_completed_replay_is_byte_identical_without_rerunning_llm() -> None:
    harness = await build_harness()
    command = make_command()

    first = await harness.turn_service.execute(command)
    calls_after_first = harness.model.generate_calls
    second = await harness.turn_service.execute(command)

    assert first.status_code == second.status_code == 200
    # Byte-identical replay of the stored snapshot; the LLM is never re-run.
    assert second.body_json == first.body_json
    assert harness.model.generate_calls == calls_after_first == 1


async def test_in_progress_returns_202_with_retry_after() -> None:
    harness = await build_harness()
    command = make_command()
    # A live PROCESSING claim held by another owner.
    await _preclaim(harness, command)

    result = await harness.turn_service.execute(command)

    assert result.status_code == 202
    assert result.body_json is None
    assert result.retry_after_seconds == 3
    # The in-progress turn was never touched; the LLM never ran.
    assert harness.model.generate_calls == 0


async def test_hash_conflict_returns_409() -> None:
    harness = await build_harness()
    command = make_command()
    # An existing receipt for the same id but a materially different request.
    await _preclaim(harness, command, canonical_hash="a" * 64)

    result = await harness.turn_service.execute(command)

    assert result.status_code == 409
    body = json.loads(result.body_json)
    assert body["error"]["code"] == "idempotency_conflict"
    assert body["error"]["retryable"] is False
    assert harness.model.generate_calls == 0


# --- guards + terminal replay ---------------------------------------------- #


async def test_operator_pairing_failure_is_opaque_404_and_replays() -> None:
    harness = await build_harness()
    command = make_command(tenant_id="tenant-WRONG")

    first = await harness.turn_service.execute(command)
    second = await harness.turn_service.execute(command)

    assert first.status_code == 404
    body = json.loads(first.body_json)
    assert body["error"]["code"] == "not_found"
    assert body["error"]["retryable"] is False
    # Stable terminal replay — identical bytes, no conversation, no LLM.
    assert second.status_code == 404
    assert second.body_json == first.body_json
    assert harness.model.generate_calls == 0


async def test_character_not_in_roster_is_opaque_404() -> None:
    harness = await build_harness()
    command = make_command(character_id="ghost-character")

    result = await harness.turn_service.execute(command)

    assert result.status_code == 404
    assert json.loads(result.body_json)["error"]["code"] == "not_found"
    assert harness.model.generate_calls == 0


async def test_subscription_denied_is_opaque_404() -> None:
    # A locked tenant empties the roster → the character is not chattable.
    harness = await build_harness(locked_tenants=("tenant-A",))
    command = make_command()

    result = await harness.turn_service.execute(command)

    assert result.status_code == 404
    assert json.loads(result.body_json)["error"]["code"] == "not_found"
    assert harness.model.generate_calls == 0


async def test_conversation_character_mismatch_is_terminal_409() -> None:
    harness = await build_harness()
    # A conversation that belongs to a different character.
    other = Conversation.start(character_id="other-character", source="line")
    await harness.conversation_repo.save(other)
    command = make_command(
        conversation_id=other.id, conversation_id_present=True,
    )

    first = await harness.turn_service.execute(command)
    second = await harness.turn_service.execute(command)

    assert first.status_code == 409
    body = json.loads(first.body_json)
    assert body["error"]["code"] == "conversation_character_mismatch"
    assert body["error"]["retryable"] is False
    # Stable, replayable terminal body.
    assert second.status_code == 409
    assert second.body_json == first.body_json
    assert harness.model.generate_calls == 0


async def test_self_heal_records_conversation_and_completes() -> None:
    harness = await build_harness()
    command = make_command()  # no conversation_id → self-heal

    result = await harness.turn_service.execute(command)

    body = json.loads(result.body_json)
    receipt = await harness.receipt_repo.get(body["turn_id"])
    # The self-healed conversation id was written back to the receipt so a
    # retry reuses it (proven byte-identical by the replay test).
    assert receipt.conversation_id == body["conversation_id"]
    conversation = await harness.conversation_repo.get(body["conversation_id"])
    assert conversation is not None
    assert conversation.source == "line"


# --- execution exception mapping ------------------------------------------- #


class _RaisingChatService:
    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    async def send_message(self, request, *, current_user_id=None, external_turn=None):
        raise self._exc


async def test_runtime_limit_maps_to_429_failed_retryable() -> None:
    harness = await build_harness(
        chat_service_override=_RaisingChatService(ChatRuntimeLimitExceeded()),
    )
    command = make_command()

    result = await harness.turn_service.execute(command)

    assert result.status_code == 429
    body = json.loads(result.body_json)
    assert body["error"]["retryable"] is True
    receipt_id = next(iter(harness.receipt_repo._records))
    receipt = await harness.receipt_repo.get(receipt_id)
    assert receipt.state is ExternalChatTurnState.FAILED_RETRYABLE


async def test_generic_exception_maps_to_503_failed_retryable() -> None:
    harness = await build_harness(
        chat_service_override=_RaisingChatService(RuntimeError("boom")),
    )
    command = make_command()

    result = await harness.turn_service.execute(command)

    assert result.status_code == 503
    body = json.loads(result.body_json)
    assert body["error"]["retryable"] is True
    receipt_id = next(iter(harness.receipt_repo._records))
    receipt = await harness.receipt_repo.get(receipt_id)
    assert receipt.state is ExternalChatTurnState.FAILED_RETRYABLE


# --- response-snapshot attachment normalisation ---------------------------- #


async def test_response_snapshot_normalises_and_stats_attachments() -> None:
    storage = FakeObjectStorage(stats={
        "users/u1/pic.png": ObjectMetadata(
            object_key="users/u1/pic.png",
            url="/v1/public/users/u1/pic.png",
            content_type="image/png",
            size_bytes=1234,
            sha256="a" * 64,
        ),
    })
    assistant = Message(
        role=MessageRole.ASSISTANT,
        content="看看這張",
        attachments=(
            MessageAttachment(
                kind="image", url="/v1/public/users/u1/pic.png",
                mime_type="image/png",
            ),
        ),
    )
    view = RosterCharacterView(
        character_id=CHARACTER_ID, display_name="Airi",
        background_frozen=False, avatar_object_ref=None,
        presentation_version="v1",
    )

    snapshot = await build_response_snapshot(
        object_storage=storage, character_view=view,
        request_id="r1", turn_id="t1", conversation_id="c1",
        assistant_message=assistant,
    )

    assert snapshot["attachments"] == [{
        "object_ref": "users/u1/pic.png",
        "media_type": "image/png",
        "size_bytes": 1234,
        "sha256": "a" * 64,
    }]


async def test_response_snapshot_skips_unstattable_attachment() -> None:
    # No stat metadata seeded → the attachment cannot be authoritative → skip.
    storage = FakeObjectStorage(stats={})
    assistant = Message(
        role=MessageRole.ASSISTANT,
        content="text",
        attachments=(
            MessageAttachment(kind="image", url="/v1/public/missing.png"),
        ),
    )
    view = RosterCharacterView(
        character_id=CHARACTER_ID, display_name="Airi",
        background_frozen=False, avatar_object_ref=None,
        presentation_version="v1",
    )

    snapshot = await build_response_snapshot(
        object_storage=storage, character_view=view,
        request_id="r1", turn_id="t1", conversation_id="c1",
        assistant_message=assistant,
    )

    assert snapshot["attachments"] == []
