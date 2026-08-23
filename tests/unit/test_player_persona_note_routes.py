"""PP2 — ``GET`` / ``PUT /characters/{id}/player-persona-note``."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.api.dependencies import get_container, get_current_user_id
from kokoro_link.api.routes.player_persona_note import router
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.application.services.player_persona_note_service import (
    PlayerPersonaNoteService,
)
from kokoro_link.domain.entities.player_persona_note import (
    PLAYER_PERSONA_NOTE_MAX_CHARS,
)
from kokoro_link.infrastructure.repositories.in_memory_player_persona_notes import (
    InMemoryPlayerPersonaNoteRepository,
)

_TEST_USER_ID = "alice"
_PATH = "/api/v1/characters/c1/player-persona-note"


@dataclass
class _Container:
    player_persona_note_service: PlayerPersonaNoteService | None
    character_service: object | None = None  # None → ownership passes through


def _client(container: _Container) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.dependency_overrides[get_current_user_id] = lambda: _TEST_USER_ID
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _wired() -> tuple[_Container, InMemoryPlayerPersonaNoteRepository]:
    repository = InMemoryPlayerPersonaNoteRepository()
    return (
        _Container(
            player_persona_note_service=PlayerPersonaNoteService(repository),
        ),
        repository,
    )


def test_get_returns_empty_note_when_never_declared() -> None:
    container, _ = _wired()

    resp = _client(container).get(_PATH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["note"] == ""
    assert body["updated_at"] is None
    assert body["operator_id"] == _TEST_USER_ID


def test_put_then_get_returns_the_declaration() -> None:
    container, _ = _wired()
    client = _client(container)

    put = client.put(_PATH, json={"note": "  我是超能力者  "})

    assert put.status_code == 200
    assert put.json()["note"] == "我是超能力者"
    assert put.json()["updated_at"] is not None

    got = client.get(_PATH)
    assert got.json()["note"] == "我是超能力者"


def test_put_empty_string_clears_the_declaration() -> None:
    container, repository = _wired()
    client = _client(container)
    client.put(_PATH, json={"note": "我是超能力者"})

    cleared = client.put(_PATH, json={"note": ""})

    assert cleared.status_code == 200
    assert cleared.json()["note"] == ""
    assert client.get(_PATH).json()["note"] == ""
    import asyncio

    assert asyncio.run(
        repository.get(character_id="c1", operator_id=_TEST_USER_ID),
    ) is None


def test_put_over_the_ceiling_is_422() -> None:
    container, repository = _wired()

    resp = _client(container).put(
        _PATH, json={"note": "我" * (PLAYER_PERSONA_NOTE_MAX_CHARS + 1)},
    )

    assert resp.status_code == 422
    import asyncio

    assert asyncio.run(
        repository.get(character_id="c1", operator_id=_TEST_USER_ID),
    ) is None


def test_put_at_the_ceiling_is_accepted() -> None:
    container, _ = _wired()

    resp = _client(container).put(
        _PATH, json={"note": "我" * PLAYER_PERSONA_NOTE_MAX_CHARS},
    )

    assert resp.status_code == 200


def test_service_unwired_is_503() -> None:
    container = _Container(player_persona_note_service=None)
    client = _client(container)

    assert client.get(_PATH).status_code == 503
    assert client.put(_PATH, json={"note": "我是超能力者"}).status_code == 503


@pytest.fixture
def auth_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, str, str]]:
    """The real assembled app with auth on.

    Doubles as the wiring pin: the stub-container tests above would still
    pass if the router were never included or the container never built a
    ``player_persona_note_service``.
    """
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "true")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv(
        "KOKORO_JWT_SECRET",
        "player-persona-note-route-secret-at-least-32-bytes",
    )
    app = create_app()
    container = app.state.container

    async def seed() -> str:
        await container.operator_profile_repository.save(
            OperatorProfile(
                id="player",
                display_name="Player",
                email="player@example.com",
                password_hash="test",
            ),
        )
        character = await container.character_service.create_character(
            CreateCharacterRequest(name="澄香"), user_id="player",
        )
        return character.id

    character_id = asyncio.run(seed())
    token = container.jwt_service.encode("player")
    with TestClient(app) as client:
        yield client, token, character_id


def test_unauthenticated_request_is_rejected(
    auth_app: tuple[TestClient, str, str],
) -> None:
    client, _token, character_id = auth_app
    path = f"/api/v1/characters/{character_id}/player-persona-note"

    assert client.get(path).status_code in (401, 403)
    assert client.put(
        path, json={"note": "我是超能力者"},
    ).status_code in (401, 403)


def test_authenticated_round_trip_through_the_assembled_app(
    auth_app: tuple[TestClient, str, str],
) -> None:
    client, token, character_id = auth_app
    path = f"/api/v1/characters/{character_id}/player-persona-note"
    headers = {"Authorization": f"Bearer {token}"}

    put = client.put(path, json={"note": "我是超能力者"}, headers=headers)

    assert put.status_code == 200, put.text
    assert client.get(path, headers=headers).json()["note"] == "我是超能力者"
