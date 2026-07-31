"""HTTP contract for a conversation that already has a turn in flight.

``ConversationBusyError`` must surface as ``409`` with a structured
``conversation_busy`` code on BOTH chat entry points, without disturbing the
existing 403 / 404 / 429 mappings.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.chat import router
from kokoro_link.application.services.chat_service import (
    ChatRuntimeLimitExceeded,
)
from kokoro_link.application.services.chat_turn_lease import ConversationBusyError
from kokoro_link.contracts.cloud_gateway import CloudGatewayRequestTooLargeError

_CONVERSATION_ID = "conv-1"
_CHARACTER_ID = "char-1"


class _CharacterService:
    async def get_character_entity(self, character_id: str, *, user_id: str = ""):
        return SimpleNamespace(id=character_id, user_id=user_id or "default")


class _BusyChatService:
    async def send_message(self, payload, **_kwargs):  # noqa: ANN001
        raise ConversationBusyError(_CONVERSATION_ID)

    async def send_message_stream(self, payload, **_kwargs):  # noqa: ANN001
        raise ConversationBusyError(_CONVERSATION_ID)


class _RuntimeLimitedChatService:
    async def send_message(self, payload, **_kwargs):  # noqa: ANN001
        raise ChatRuntimeLimitExceeded("session message limit reached")

    async def send_message_stream(self, payload, **_kwargs):  # noqa: ANN001
        raise ChatRuntimeLimitExceeded("session message limit reached")


class _OversizeChatService:
    async def send_message(self, payload, **_kwargs):  # noqa: ANN001
        raise CloudGatewayRequestTooLargeError(
            actual_bytes=229_377,
            max_bytes=229_376,
        )

    async def send_message_stream(self, payload, **_kwargs):  # noqa: ANN001
        raise CloudGatewayRequestTooLargeError(
            actual_bytes=229_377,
            max_bytes=229_376,
        )


def _client(chat_service) -> TestClient:  # noqa: ANN001
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        chat_service=chat_service,
        character_service=_CharacterService(),
        conversation_repository=None,
        turn_undo_service=None,
        object_storage=None,
        app_settings=None,
        operator_profile_repository=None,
    )
    return TestClient(app)


def _payload() -> dict:
    return {
        "character_id": _CHARACTER_ID,
        "conversation_id": _CONVERSATION_ID,
        "message": "hi",
    }


@pytest.mark.parametrize(
    "path",
    ["/api/v1/chat/messages", "/api/v1/chat/messages/stream"],
)
def test_conversation_busy_maps_to_409_with_structured_code(path: str) -> None:
    client = _client(_BusyChatService())

    response = client.post(path, json=_payload())

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "conversation_busy"
    assert detail["conversation_id"] == _CONVERSATION_ID


@pytest.mark.parametrize(
    "path",
    ["/api/v1/chat/messages", "/api/v1/chat/messages/stream"],
)
def test_chat_payload_too_large_is_structured_non_retryable_413(
    path: str,
) -> None:
    client = _client(_OversizeChatService())

    response = client.post(path, json=_payload())

    assert response.status_code == 413
    assert response.json() == {
        "detail": {
            "code": "payload_too_large",
            "message": "LLM request exceeds the size limit",
            "retryable": False,
        },
    }


class _RecordingFinalizer:
    def __init__(self) -> None:
        self.releases = 0
        self.finished = 0

    @property
    def conversation_id(self) -> str:
        return _CONVERSATION_ID

    async def finish(self, assistant_text: str):  # noqa: ANN201
        self.finished += 1
        raise AssertionError("finish must not run when the stream fails")

    async def release_turn_lease(self) -> None:
        self.releases += 1


class _FailingStreamChatService:
    def __init__(self, finalizer: _RecordingFinalizer) -> None:
        self._finalizer = finalizer

    async def send_message_stream(self, payload, **_kwargs):  # noqa: ANN001
        async def _stream():  # noqa: ANN202
            yield "partial"
            raise RuntimeError("upstream exploded mid-stream")

        return _stream(), self._finalizer


def test_stream_that_never_finishes_releases_the_turn_lease() -> None:
    finalizer = _RecordingFinalizer()
    client = _client(_FailingStreamChatService(finalizer))

    with pytest.raises(RuntimeError, match="upstream exploded mid-stream"):
        client.post("/api/v1/chat/messages/stream", json=_payload())

    assert finalizer.finished == 0
    # Without this the conversation would stay claimed until the lease TTL.
    assert finalizer.releases == 1


@pytest.mark.parametrize(
    "path",
    ["/api/v1/chat/messages", "/api/v1/chat/messages/stream"],
)
def test_existing_runtime_limit_mapping_is_untouched(path: str) -> None:
    client = _client(_RuntimeLimitedChatService())

    response = client.post(path, json=_payload())

    assert response.status_code == 429, response.text
