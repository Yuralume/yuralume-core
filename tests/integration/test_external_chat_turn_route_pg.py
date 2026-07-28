"""PostgreSQL integration for the external-chat turn service + route (LH2).

Drives the REAL :class:`ExternalChatTurnService` over the SA receipt repo and
the SA conversation repo on Postgres, so the guarantees the in-memory twins
cannot prove hold under true overlapping transactions:

* the route function durably accepts with 202, then polling the same
  request id replays 200 after the background driver persists a
  ``source="line"`` conversation + user/assistant rows in PG;
* two concurrent deliveries of the same ``request_id`` resolve to exactly one
  200 (the driver) and one 202 (in-progress), never two drives;
* an expired-lease GENERATED receipt is taken over and finalised from its
  stored snapshot WITHOUT re-running the LLM.

Requires Docker (testcontainers); skipped automatically when unavailable.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from types import SimpleNamespace

import pytest
from sqlalchemy import update

from kokoro_link.api.routes.external_chat import (
    ExternalChatTurnRequestModel,
    create_external_chat_turn,
)
from kokoro_link.contracts.external_chat_execution import GeneratedTurnSnapshot
from kokoro_link.contracts.external_chat_turn import ExternalChatTurnState
from kokoro_link.domain.entities.conversation import Message, MessageRole
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.persistence.models import (
    ExternalChatTurnReceiptRow,
)
from kokoro_link.infrastructure.persistence.sa_conversation_repository import (
    SAConversationRepository,
)
from kokoro_link.infrastructure.persistence.sa_external_chat_turn_repository import (  # noqa: E501
    SAExternalChatTurnReceiptRepository,
)

from tests.unit.external_chat_turn_harness import (
    ACCOUNT,
    CHARACTER_ID,
    TENANT,
    CountingFakeChatModel,
    build_harness,
    make_command,
)

pytestmark = pytest.mark.asyncio


class _SlowCountingModel(FakeChatModel):
    """Counts calls and holds the lease briefly so a concurrent delivery
    reliably observes the in-flight PROCESSING receipt."""

    def __init__(self, provider_id: str = "fake", delay: float = 0.25) -> None:
        super().__init__(provider_id)
        self.generate_calls = 0
        self._delay = delay

    async def generate(
        self, prompt: str, *, image_urls: Sequence[str] = (), model: str | None = None,
    ) -> str:
        self.generate_calls += 1
        await asyncio.sleep(self._delay)
        return await super().generate(prompt, image_urls=image_urls, model=model)


async def _sa_harness(session_factory, *, model=None):
    receipt_repo = SAExternalChatTurnReceiptRepository(session_factory)
    conversation_repo = SAConversationRepository(session_factory)
    return await build_harness(
        receipt_repo=receipt_repo,
        conversation_repo=conversation_repo,
        model=model or CountingFakeChatModel(),
    )


def _request_model(command) -> ExternalChatTurnRequestModel:
    return ExternalChatTurnRequestModel(
        request_id=command.request_id,
        tenant_id=command.tenant_id,
        account_id=command.account_id,
        character_id=command.character_id,
        conversation_id=command.conversation_id,
        message=command.message,
        attachments=[],
        channel="line",
        contract_version=1,
    )


async def test_route_happy_path_persists_line_conversation_in_pg(
    session_factory,
) -> None:
    harness = await _sa_harness(session_factory)
    container = SimpleNamespace(external_chat_turn_service=harness.turn_service)
    command = make_command(request_id="line:pg:evt-happy-1")

    first = await create_external_chat_turn(
        request=_request_model(command), container=container,
    )

    assert first.status_code == 202
    assert first.headers["retry-after"] == "3"
    assert first.body == b""

    try:
        for _ in range(200):
            response = await create_external_chat_turn(
                request=_request_model(command), container=container,
            )
            if response.status_code == 200:
                break
            assert response.status_code == 202
            await asyncio.sleep(0.01)
        else:  # pragma: no cover - local PG completion is deterministic
            raise AssertionError("background PG turn did not complete")

        body = json.loads(response.body)
        assert body["request_id"] == command.request_id
        assert body["character"]["character_id"] == CHARACTER_ID
        assert harness.model.generate_calls == 1

        # The turn is durably COMPLETED and the conversation lives in PG.
        receipt = await harness.receipt_repo.get(body["turn_id"])
        assert receipt.state is ExternalChatTurnState.COMPLETED
        conversation = await harness.conversation_repo.get(body["conversation_id"])
        assert conversation.source == "line"
        assert [m.role for m in conversation.messages] == [
            MessageRole.USER, MessageRole.ASSISTANT,
        ]
    finally:
        await harness.turn_service.stop()


async def test_concurrent_same_request_one_200_one_202(session_factory) -> None:
    harness = await _sa_harness(
        session_factory, model=_SlowCountingModel(delay=0.3),
    )
    command = make_command(request_id="line:pg:evt-concurrent-1")

    first, second = await asyncio.gather(
        harness.turn_service.execute(command),
        harness.turn_service.execute(command),
    )

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [200, 202]
    # Exactly one delivery drove the turn (one LLM call).
    assert harness.model.generate_calls == 1
    # The 202 carries the retry hint; the 200 carries the snapshot.
    in_progress = first if first.status_code == 202 else second
    completed = first if first.status_code == 200 else second
    assert in_progress.retry_after_seconds == 3
    assert json.loads(completed.body_json)["request_id"] == command.request_id


class _CheckpointGeneratedThenStop:
    """Persists user + GENERATED checkpoint then returns without committing —
    the orchestrator leaves the receipt GENERATED (a real crash)."""

    def __init__(self, conversation_repo, assistant_text: str) -> None:
        self._conversations = conversation_repo
        self._assistant_text = assistant_text

    async def send_message(self, request, *, current_user_id=None, external_turn=None):
        conversation = await self._conversations.get(request.conversation_id)
        user_message = Message(role=MessageRole.USER, content=request.message)
        await external_turn.persist_user_turn(conversation, user_message)
        await external_turn.checkpoint_generated(
            GeneratedTurnSnapshot(
                turn_id=external_turn.stable_turn_id(),
                assistant_text=self._assistant_text,
                attachments=(),
                assistant_kind="chat",
                content_mode="normal",
            ),
        )


async def test_expired_lease_takeover_finalizes_without_llm(session_factory) -> None:
    receipt_repo = SAExternalChatTurnReceiptRepository(session_factory)
    conversation_repo = SAConversationRepository(session_factory)
    command = make_command(request_id="line:pg:evt-recover-1")

    # First delivery reaches a durable GENERATED then "crashes" (no commit).
    crash = await build_harness(
        receipt_repo=receipt_repo,
        conversation_repo=conversation_repo,
        chat_service_override=_CheckpointGeneratedThenStop(
            conversation_repo, "生成好的回覆",
        ),
    )
    first = await crash.turn_service.execute(command)
    assert first.status_code == 503

    # Find the receipt and expire its lease in PG (real takeover-eligibility).
    seed = await receipt_repo.claim_or_get(
        authenticated_caller="cloud-channel",
        request_id=command.request_id,
        canonical_request_hash="probe",  # different hash → HashConflict, but
        canonical_hash_version=1,          # we only need the row id via a read
        contract_version=1,
        tenant_id=TENANT,
        account_id=ACCOUNT,
        character_id=CHARACTER_ID,
        requested_conversation_id=None,
    )
    turn_id = seed.receipt.turn_id
    receipt = await receipt_repo.get(turn_id)
    assert receipt.state is ExternalChatTurnState.GENERATED
    async with session_factory() as session:
        from datetime import datetime, timezone
        await session.execute(
            update(ExternalChatTurnReceiptRow)
            .where(ExternalChatTurnReceiptRow.turn_id == turn_id)
            .values(lease_until=datetime(2000, 1, 1, tzinfo=timezone.utc)),
        )
        await session.commit()

    # Retry with a counting LLM: GeneratedRecovery finalises from snapshot.
    model = CountingFakeChatModel()
    retry = await build_harness(
        receipt_repo=receipt_repo,
        conversation_repo=conversation_repo,
        model=model,
    )
    result = await retry.turn_service.execute(command)

    assert result.status_code == 200
    assert model.generate_calls == 0
    receipt = await receipt_repo.get(turn_id)
    assert receipt.state is ExternalChatTurnState.COMPLETED
    conversation = await conversation_repo.get(receipt.conversation_id)
    assert [m.role for m in conversation.messages] == [
        MessageRole.USER, MessageRole.ASSISTANT,
    ]
    assert conversation.messages[1].content == "生成好的回覆"
