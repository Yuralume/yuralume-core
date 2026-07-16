from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.characters import router as character_router
from kokoro_link.application.dto.character import (
    CreateCharacterRequest,
    proactive_rhythm_from_values,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


def _client(service: CharacterService) -> TestClient:
    class _Container:
        pass

    container = _Container()
    container.character_service = service

    app = FastAPI()
    app.state.container = container
    app.include_router(character_router, prefix="/api/v1")
    return TestClient(app)


def test_proactive_rhythm_classifies_existing_numeric_settings() -> None:
    assert proactive_rhythm_from_values(daily_limit=1, cooldown_minutes=180) == "quiet"
    assert proactive_rhythm_from_values(daily_limit=0, cooldown_minutes=30) == "quiet"
    assert proactive_rhythm_from_values(daily_limit=3, cooldown_minutes=30) == "balanced"
    assert proactive_rhythm_from_values(daily_limit=6, cooldown_minutes=15) == "lively"


@pytest.mark.asyncio
async def test_player_proactive_rhythm_route_maps_preset_to_character_fields() -> None:
    service = CharacterService(InMemoryCharacterRepository())
    created = await service.create_character(CreateCharacterRequest(name="Mio"))
    client = _client(service)

    response = client.patch(
        f"/api/v1/characters/{created.id}/proactive-rhythm",
        json={"rhythm": "quiet"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["proactive_rhythm"] == "quiet"
    assert body["proactive_daily_limit"] == 1
    assert body["proactive_cooldown_minutes"] == 180


@pytest.mark.asyncio
async def test_player_proactive_rhythm_route_rejects_unknown_preset() -> None:
    service = CharacterService(InMemoryCharacterRepository())
    created = await service.create_character(CreateCharacterRequest(name="Mio"))
    client = _client(service)

    response = client.patch(
        f"/api/v1/characters/{created.id}/proactive-rhythm",
        json={"rhythm": "always"},
    )

    assert response.status_code == 422
