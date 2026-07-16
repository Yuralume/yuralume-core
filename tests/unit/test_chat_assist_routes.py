from __future__ import annotations

from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.chat_assist import router
from kokoro_link.application.dto.chat_assist import (
    ChatAssistSuggestion,
    ChatAssistSuggestionsResponse,
)
from kokoro_link.application.services.chat_assist_service import (
    ChatAssistCharacterNotFoundError,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID


class _ChatAssistService:
    def __init__(self, response: ChatAssistSuggestionsResponse | None = None) -> None:
        self.response = response or ChatAssistSuggestionsResponse()
        self.calls: list[tuple[str, str, int]] = []

    async def suggest(
        self,
        character_id: str,
        *,
        user_id: str = DEFAULT_OPERATOR_ID,
        count: int = 4,
    ) -> ChatAssistSuggestionsResponse:
        self.calls.append((character_id, user_id, count))
        return self.response


class _MissingCharacterService(_ChatAssistService):
    async def suggest(
        self,
        character_id: str,
        *,
        user_id: str = DEFAULT_OPERATOR_ID,
        count: int = 4,
    ) -> ChatAssistSuggestionsResponse:
        raise ChatAssistCharacterNotFoundError("missing")


def _client(chat_assist_service) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.container = SimpleNamespace(
        chat_assist_service=chat_assist_service,
        app_settings=None,
        operator_profile_repository=None,
    )
    return TestClient(app)


def test_chat_assist_suggestions_route_returns_generated_lines() -> None:
    service = _ChatAssistService(
        ChatAssistSuggestionsResponse(
            suggestions=[
                ChatAssistSuggestion(
                    text="Want to ask how your afternoon is going?",
                    reason="Uses the current schedule.",
                ),
            ],
        ),
    )
    client = _client(service)

    response = client.post(
        "/api/v1/characters/char-1/chat-assist/suggestions",
        json={"count": 3},
    )

    assert response.status_code == 200
    assert response.json() == {
        "suggestions": [
            {
                "text": "Want to ask how your afternoon is going?",
                "reason": "Uses the current schedule.",
            },
        ],
    }
    assert service.calls == [("char-1", DEFAULT_OPERATOR_ID, 3)]


def test_chat_assist_suggestions_route_returns_404_for_missing_character() -> None:
    client = _client(_MissingCharacterService())

    response = client.post(
        "/api/v1/characters/missing/chat-assist/suggestions",
        json={"count": 2},
    )

    assert response.status_code == 404


def test_chat_assist_suggestions_route_returns_503_when_service_unavailable() -> None:
    client = _client(None)

    response = client.post(
        "/api/v1/characters/char-1/chat-assist/suggestions",
        json={"count": 2},
    )

    assert response.status_code == 503
