"""HTTP contract tests for character identity fields."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.application.dto.character import UpdateCharacterRequest
from kokoro_link.application.services.character_primary_image_initializer import (
    CharacterPrimaryImageInitializationResult,
)


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    app = create_app()
    app.state.container.character_runtime_initializer = None
    app.state.container.proactive_scheduler = None
    app.state.container.world_event_scheduler = None
    app.state.container.telegram_polling_service = None
    with TestClient(app) as test_client:
        yield test_client


def test_character_identity_fields_round_trip_through_http(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/characters",
        json={
            "name": "Ren",
            "gender_identity": "男性",
            "third_person_pronoun": "他",
            "visual_gender_presentation": "masculine young man",
            "visual_subject_type": "human",
            "visual_generation_style": "realistic",
        },
    )

    assert created.status_code == 201
    payload = created.json()
    character_id = payload["id"]
    assert payload["gender_identity"] == "男性"
    assert payload["third_person_pronoun"] == "他"
    assert payload["visual_gender_presentation"] == "masculine young man"
    assert payload["visual_subject_type"] == "human"
    assert payload["visual_generation_style"] == "realistic"

    fetched = client.get(f"/api/v1/characters/{character_id}")
    assert fetched.status_code == 200
    assert fetched.json()["gender_identity"] == "男性"
    assert fetched.json()["visual_subject_type"] == "human"
    assert fetched.json()["visual_generation_style"] == "realistic"

    cleared = client.patch(
        f"/api/v1/characters/{character_id}",
        json={
            "gender_identity": "",
            "third_person_pronoun": "",
            "visual_gender_presentation": "",
            "visual_subject_type": "auto",
            "visual_generation_style": "",
        },
    )

    assert cleared.status_code == 200
    assert cleared.json()["gender_identity"] == ""
    assert cleared.json()["third_person_pronoun"] == ""
    assert cleared.json()["visual_gender_presentation"] == ""
    assert cleared.json()["visual_subject_type"] == "auto"
    assert cleared.json()["visual_generation_style"] == ""


def test_create_character_response_includes_auto_primary_image(
    client: TestClient,
) -> None:
    container = client.app.state.container

    class _FakePrimaryImageInitializer:
        async def ensure_after_create(
            self,
            character_id: str,
            *,
            user_id: str | None,
        ) -> CharacterPrimaryImageInitializationResult:
            entity = await container.character_service.get_character_entity(
                character_id,
                user_id=user_id,
            )
            assert entity is not None
            assert entity.visual_generation_style == "realistic"
            character = await container.character_service.update_character(
                character_id,
                UpdateCharacterRequest(
                    image_urls=[f"/uploads/characters/{character_id}/auto.png"],
                ),
                user_id=user_id,
            )
            assert character is not None
            return CharacterPrimaryImageInitializationResult(
                character_id=character_id,
                character=character,
                image_generated=True,
            )

    container.character_primary_image_initializer = _FakePrimaryImageInitializer()

    created = client.post(
        "/api/v1/characters",
        json={"name": "Airi", "visual_generation_style": "realistic"},
    )

    assert created.status_code == 201
    payload = created.json()
    assert payload["image_urls"] == [
        f"/uploads/characters/{payload['id']}/auto.png",
    ]
    assert payload["visual_generation_style"] == "realistic"
