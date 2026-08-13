"""HTTP contract for the album API's write paths onto managed characters
(EC2-C).

``AlbumPanel``'s "promote to stage" button calls ``POST
/api/v1/album/{item_id}/promote`` directly — a second write path onto
``Character.image_urls`` that never goes through
``CharacterService.update_character``'s managed-write allowlist
(``managed_character_update_policy``). ``POST
/api/v1/characters/{id}/album/transfer`` writes the same field in the
other direction (removes a stage image). Both must refuse for a managed
character with the same 403 "exists, wrong kind, refuse" shape as
``CharacterCardManagedError`` (EC3), and leave ``image_urls`` untouched.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from kokoro_link.api.app import create_app
from kokoro_link.domain.entities.album_item import SOURCE_TOOL, AlbumItem
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.value_objects.character_state import CharacterState

CARD_ID = "cloud-card-mio"


def _managed_character(*, image_urls: tuple[str, ...] = ()) -> Character:
    return Character.create(
        name="美緒",
        summary="咖啡廳打工的女大生",
        user_id=DEFAULT_OPERATOR_ID,
        personality=["溫柔"],
        interests=["拉花"],
        speaking_style="輕快",
        boundaries=[],
        image_urls=list(image_urls),
        state=CharacterState(
            emotion="calm", affection=42, fatigue=0, trust=50, energy=100,
        ),
        origin_official_card_id=CARD_ID,
    )


@pytest.fixture
def client_with_managed_character(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[TestClient, Character]]:
    monkeypatch.setenv("KOKORO_AUTH_ENABLED", "false")
    monkeypatch.setenv("KOKORO_DATABASE_URL", "")
    monkeypatch.setenv("KOKORO_DEFAULT_PROVIDER_ID", "fake")
    monkeypatch.setenv("KOKORO_DEPLOYMENT_MODE", "test")
    monkeypatch.setenv("KOKORO_STORAGE_PROVIDER", "memory")
    app = create_app()
    container = app.state.container
    container.character_runtime_initializer = None
    container.proactive_scheduler = None
    container.world_event_scheduler = None
    container.telegram_polling_service = None

    managed = _managed_character(
        image_urls=("/uploads/characters/managed-mio/stage.png",),
    )
    assert container.character_repository is not None
    asyncio.run(container.character_repository.save(managed))

    with TestClient(app) as test_client:
        yield test_client, managed


def _seed_album_item(container, character_id: str, *, url: str) -> AlbumItem:
    assert container.album_repository is not None
    item = AlbumItem.create(character_id=character_id, url=url, source=SOURCE_TOOL)
    asyncio.run(container.album_repository.add(item))
    return item


def test_promote_to_stage_refuses_managed_character(
    client_with_managed_character: tuple[TestClient, Character],
) -> None:
    client, managed = client_with_managed_character
    container = client.app.state.container
    item = _seed_album_item(
        container,
        managed.id,
        url=f"/uploads/characters/{managed.id}/tools/fan-art.png",
    )

    response = client.post(f"/api/v1/album/{item.id}/promote")

    assert response.status_code == 403
    stored = asyncio.run(container.character_repository.get(managed.id))
    assert stored is not None
    assert stored.image_urls == managed.image_urls
    # The album row must survive the refusal — nothing was consumed.
    assert asyncio.run(container.album_repository.get(item.id)) is not None


def test_transfer_from_stage_refuses_managed_character(
    client_with_managed_character: tuple[TestClient, Character],
) -> None:
    client, managed = client_with_managed_character
    container = client.app.state.container
    stage_url = managed.image_urls[0]

    response = client.post(
        f"/api/v1/characters/{managed.id}/album/transfer",
        json={"url": stage_url},
    )

    assert response.status_code == 403
    stored = asyncio.run(container.character_repository.get(managed.id))
    assert stored is not None
    assert stored.image_urls == managed.image_urls


def test_ordinary_character_album_write_paths_are_unchanged(
    client_with_managed_character: tuple[TestClient, Character],
) -> None:
    """Self-host / non-managed regression check: both endpoints keep
    working exactly as before this ticket."""
    client, _managed = client_with_managed_character
    container = client.app.state.container

    ordinary = Character.create(
        name="蓮",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        image_urls=["/uploads/characters/ren/stage.png"],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    asyncio.run(container.character_repository.save(ordinary))

    transfer = client.post(
        f"/api/v1/characters/{ordinary.id}/album/transfer",
        json={"url": ordinary.image_urls[0]},
    )
    assert transfer.status_code == 200
    assert transfer.json()["image_urls"] == []

    item = _seed_album_item(
        container,
        ordinary.id,
        url=f"/uploads/characters/{ordinary.id}/tools/fan-art.png",
    )
    promote = client.post(f"/api/v1/album/{item.id}/promote")
    assert promote.status_code == 200
    assert promote.json()["image_urls"] == [item.url]
