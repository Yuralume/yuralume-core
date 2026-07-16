"""BDD for CharacterImageService + the image upload/delete routes.

Live2D is gone; characters now carry 1–N uploaded portrait URLs. The
service hides the storage layout so the route stays thin, and the DB
is the source of truth: the URL list on the character is what the UI
trusts even if an object went missing.
"""

from __future__ import annotations

import io
from collections.abc import Mapping
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.characters import router as character_router
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_image_service import (
    CharacterImageService,
    CharacterImageError,
    ImageNotFoundError,
    ImageTooLargeError,
    MAX_IMAGES_PER_CHARACTER,
    TooManyImagesError,
    UnsupportedImageTypeError,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessLocked,
)
from kokoro_link.contracts.image_provider import ImageTokenUsage
from kokoro_link.contracts.object_storage import StoredObject
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_generation_usage import (
    InMemoryGenerationUsageRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.usage.recorder import BackgroundUsageEventRecorder
from tests.unit._image_provider_stub import StaticActiveImageProvider


def _build(tmp_path: Path) -> tuple[
    CharacterImageService, CharacterService, InMemoryCharacterRepository,
]:
    char_repo = InMemoryCharacterRepository()
    character_service = CharacterService(char_repo)
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    service = CharacterImageService(
        character_repository=char_repo,
        uploads_dir=tmp_path,
        object_storage=storage,
    )
    return service, character_service, char_repo


async def _seed_character(character_service: CharacterService) -> str:
    created = await character_service.create_character(CreateCharacterRequest(name="Mio"))
    return created.id


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _GeneratingProvider:
    provider_id = "stub-image"

    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = outputs
        self.calls: list[dict] = []

    async def generate(self, **kwargs):  # noqa: ANN003
        self.calls.append(dict(kwargs))
        return list(self.outputs)


class _DenySubscriptionGuard:
    async def ensure_character_allowed(self, character) -> None:
        raise SubscriptionAccessLocked("tenant-a")


@pytest.mark.asyncio
async def test_subscription_lock_blocks_image_before_provider_call(
    tmp_path: Path,
) -> None:
    characters = InMemoryCharacterRepository()
    character_service = CharacterService(characters)
    character_id = await _seed_character(character_service)
    provider = _GeneratingProvider([_PNG_BYTES])
    service = CharacterImageService(
        character_repository=characters,
        uploads_dir=tmp_path,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        image_provider=StaticActiveImageProvider(provider),
        subscription_access_guard=_DenySubscriptionGuard(),
    )

    with pytest.raises(SubscriptionAccessLocked):
        await service.generate_portrait(
            character_id, positive="portrait", is_primary_init=True,
        )

    assert provider.calls == []


class _FailingObjectStorage(InMemoryObjectStorage):
    async def put_bytes(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        raise RuntimeError("storage unavailable")


@pytest.mark.asyncio
async def test_add_image_stores_object_and_appends_url(tmp_path: Path) -> None:
    service, character_service, _ = _build(tmp_path)
    character_id = await _seed_character(character_service)

    updated = await service.add_image(
        character_id,
        data=_PNG_BYTES,
        mime_type="image/png",
        original_filename="avatar.png",
    )

    assert len(updated.image_urls) == 1
    url = updated.image_urls[0]
    assert url.startswith(f"/uploads/characters/{character_id}/")
    assert url.endswith(".png")

    storage = service._object_storage
    assert storage is not None
    object_key = storage.object_key_from_url(url)
    assert object_key is not None
    assert await storage.get_bytes(object_key=object_key) == _PNG_BYTES


@pytest.mark.asyncio
async def test_add_image_preserves_order_across_uploads(tmp_path: Path) -> None:
    service, character_service, _ = _build(tmp_path)
    character_id = await _seed_character(character_service)

    await service.add_image(character_id, data=_PNG_BYTES, mime_type="image/png", original_filename="a.png")
    await service.add_image(character_id, data=_PNG_BYTES, mime_type="image/jpeg", original_filename="b.jpg")
    await service.add_image(character_id, data=_PNG_BYTES, mime_type="image/webp", original_filename="c.webp")

    updated = await character_service.get_character_entity(character_id)
    assert updated is not None
    assert len(updated.image_urls) == 3
    assert updated.image_urls[0].endswith(".png")
    assert updated.image_urls[1].endswith(".jpg")
    assert updated.image_urls[2].endswith(".webp")


@pytest.mark.asyncio
async def test_add_image_rejects_oversized(tmp_path: Path) -> None:
    service, character_service, _ = _build(tmp_path)
    character_id = await _seed_character(character_service)
    huge = b"\x00" * (9 * 1024 * 1024)

    with pytest.raises(ImageTooLargeError):
        await service.add_image(
            character_id, data=huge, mime_type="image/png", original_filename="big.png",
        )


@pytest.mark.asyncio
async def test_add_image_rejects_unknown_type(tmp_path: Path) -> None:
    service, character_service, _ = _build(tmp_path)
    character_id = await _seed_character(character_service)

    with pytest.raises(UnsupportedImageTypeError):
        await service.add_image(
            character_id,
            data=_PNG_BYTES,
            mime_type="application/pdf",
            original_filename="nope.pdf",
        )


@pytest.mark.asyncio
async def test_add_image_enforces_per_character_cap(tmp_path: Path) -> None:
    service, character_service, _ = _build(tmp_path)
    character_id = await _seed_character(character_service)
    for _ in range(MAX_IMAGES_PER_CHARACTER):
        await service.add_image(
            character_id, data=_PNG_BYTES, mime_type="image/png", original_filename="a.png",
        )

    with pytest.raises(TooManyImagesError):
        await service.add_image(
            character_id, data=_PNG_BYTES, mime_type="image/png", original_filename="a.png",
        )


@pytest.mark.asyncio
async def test_remove_image_drops_url_and_object(tmp_path: Path) -> None:
    service, character_service, _ = _build(tmp_path)
    character_id = await _seed_character(character_service)
    added = await service.add_image(
        character_id, data=_PNG_BYTES, mime_type="image/png", original_filename="a.png",
    )
    url = added.image_urls[0]
    storage = service._object_storage
    assert storage is not None
    object_key = storage.object_key_from_url(url)
    assert object_key is not None
    assert await storage.stat(object_key=object_key) is not None

    updated = await service.remove_image(character_id, url=url)

    assert updated.image_urls == ()
    assert await storage.stat(object_key=object_key) is None


@pytest.mark.asyncio
async def test_remove_image_404_for_unknown_url(tmp_path: Path) -> None:
    service, character_service, _ = _build(tmp_path)
    character_id = await _seed_character(character_service)

    with pytest.raises(ImageNotFoundError):
        await service.remove_image(character_id, url="/uploads/characters/x/y.png")


@pytest.mark.asyncio
async def test_reorder_images_requires_same_set(tmp_path: Path) -> None:
    service, character_service, _ = _build(tmp_path)
    character_id = await _seed_character(character_service)
    a = await service.add_image(
        character_id, data=_PNG_BYTES, mime_type="image/png", original_filename="a.png",
    )
    b = await service.add_image(
        character_id, data=_PNG_BYTES, mime_type="image/png", original_filename="b.png",
    )
    url_a, url_b = a.image_urls[0], b.image_urls[1]

    reversed_char = await service.reorder_images(
        character_id, url_order=[url_b, url_a],
    )
    assert reversed_char.image_urls == (url_b, url_a)

    with pytest.raises(ImageNotFoundError):
        await service.reorder_images(character_id, url_order=[url_a])


@pytest.mark.asyncio
async def test_generate_candidates_records_image_usage(tmp_path: Path) -> None:
    char_repo = InMemoryCharacterRepository()
    character_service = CharacterService(char_repo)
    provider = _GeneratingProvider([_PNG_BYTES, _PNG_BYTES + b"2"])
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service = CharacterImageService(
        character_repository=char_repo,
        uploads_dir=tmp_path,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        image_provider=StaticActiveImageProvider(provider),
        usage_recorder=usage_recorder,
    )
    character_id = await _seed_character(character_service)

    _, urls = await service.generate_candidates(
        character_id,
        positive="portrait",
        aspect="square",
        count=2,
    )
    await usage_recorder.flush()

    rows = await usage_events.list_recent()
    assert len(urls) == 2
    assert len(rows) == 1
    row = rows[0]
    assert row.capability == "image"
    assert row.feature_key == "character_album_candidate"
    assert row.provider_id == "stub-image"
    assert row.profile_id == "stub"
    assert row.quantity.usage_unit == "image"
    assert row.quantity.input_quantity == 2
    assert row.quantity.output_quantity == 2
    assert row.quantity.billable_quantity == 2
    assert row.artifact_count == 2
    assert row.metadata["aspect"] == "square"


@pytest.mark.asyncio
async def test_generate_candidates_records_provider_image_token_usage(
    tmp_path: Path,
) -> None:
    char_repo = InMemoryCharacterRepository()
    character_service = CharacterService(char_repo)
    provider = _GeneratingProvider([_PNG_BYTES])
    provider.last_model_id = "gpt-image-2"
    provider.last_usage = ImageTokenUsage(
        input_tokens=120,
        input_text_tokens=20,
        input_image_tokens=100,
        output_tokens=300,
        output_image_tokens=300,
        total_tokens=420,
        estimated=False,
    )
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service = CharacterImageService(
        character_repository=char_repo,
        uploads_dir=tmp_path,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        image_provider=StaticActiveImageProvider(provider),
        usage_recorder=usage_recorder,
    )
    character_id = await _seed_character(character_service)

    await service.generate_candidates(
        character_id,
        positive="portrait",
        aspect="square",
        count=1,
    )
    await usage_recorder.flush()

    rows = await usage_events.list_recent()
    row = rows[0]
    assert row.provider_id == "stub-image"
    assert row.model_id == "gpt-image-2"
    assert row.quantity.usage_unit == "token"
    assert row.quantity.input_quantity == 120
    assert row.quantity.output_quantity == 300
    assert row.quantity.total_quantity == 420
    assert row.quantity.billable_quantity == 420
    assert row.quantity.usage_is_estimated is False
    assert row.artifact_count == 1
    assert row.metadata["artifact_quantity"] == 1
    assert row.metadata["input_text_tokens"] == 20
    assert row.metadata["input_image_tokens"] == 100
    assert row.metadata["output_image_tokens"] == 300


@pytest.mark.asyncio
async def test_generate_candidates_records_usage_when_storage_fails_after_provider_call(
    tmp_path: Path,
) -> None:
    char_repo = InMemoryCharacterRepository()
    character_service = CharacterService(char_repo)
    provider = _GeneratingProvider([_PNG_BYTES, _PNG_BYTES + b"2"])
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    service = CharacterImageService(
        character_repository=char_repo,
        uploads_dir=tmp_path,
        object_storage=_FailingObjectStorage(public_base_url="/uploads"),
        image_provider=StaticActiveImageProvider(provider),
        usage_recorder=usage_recorder,
    )
    character_id = await _seed_character(character_service)

    with pytest.raises(CharacterImageError):
        await service.generate_candidates(
            character_id,
            positive="portrait",
            aspect="square",
            count=2,
        )
    await usage_recorder.flush()

    rows = await usage_events.list_recent()
    assert len(rows) == 1
    row = rows[0]
    assert row.capability == "image"
    assert row.feature_key == "character_album_candidate"
    assert row.status == "failed"
    assert row.error_code == "RuntimeError"
    assert row.quantity.output_quantity == 2
    assert row.quantity.billable_quantity == 2
    assert row.artifact_count == 0


@pytest.mark.asyncio
async def test_remove_image_rejects_tampered_url(tmp_path: Path) -> None:
    """Path-traversal attempt should neither update DB nor touch disk."""
    service, character_service, _ = _build(tmp_path)
    character_id = await _seed_character(character_service)
    await service.add_image(
        character_id, data=_PNG_BYTES, mime_type="image/png", original_filename="a.png",
    )
    with pytest.raises(ImageNotFoundError):
        await service.remove_image(
            character_id,
            url=f"/uploads/characters/{character_id}/../../etc/passwd",
        )


# --- Route tests --------------------------------------------------------

def _client(tmp_path: Path) -> tuple[TestClient, CharacterService]:
    char_repo = InMemoryCharacterRepository()
    character_service = CharacterService(char_repo)
    image_service = CharacterImageService(
        character_repository=char_repo,
        uploads_dir=tmp_path,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
    )

    class _Container:
        pass

    container = _Container()
    container.character_service = character_service
    container.character_image_service = image_service
    container.character_draft_service = None  # draft endpoint not exercised here

    app = FastAPI()
    app.state.container = container
    app.include_router(character_router, prefix="/api/v1")
    return TestClient(app), character_service


@pytest.mark.asyncio
async def test_upload_route_returns_character_with_url(tmp_path: Path) -> None:
    client, character_service = _client(tmp_path)
    created = await character_service.create_character(
        CreateCharacterRequest(name="Rei"),
    )

    resp = client.post(
        f"/api/v1/characters/{created.id}/images",
        files={"image": ("avatar.png", io.BytesIO(_PNG_BYTES), "image/png")},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert len(body["image_urls"]) == 1
    assert body["image_urls"][0].startswith(f"/uploads/characters/{created.id}/")


@pytest.mark.asyncio
async def test_upload_route_404_for_unknown_character(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    resp = client.post(
        "/api/v1/characters/ghost/images",
        files={"image": ("a.png", io.BytesIO(_PNG_BYTES), "image/png")},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_route_removes_url(tmp_path: Path) -> None:
    client, character_service = _client(tmp_path)
    created = await character_service.create_character(
        CreateCharacterRequest(name="Rei"),
    )
    resp = client.post(
        f"/api/v1/characters/{created.id}/images",
        files={"image": ("a.png", io.BytesIO(_PNG_BYTES), "image/png")},
    )
    url = resp.json()["image_urls"][0]

    resp = client.delete(
        f"/api/v1/characters/{created.id}/images",
        params={"url": url},
    )
    assert resp.status_code == 200
    assert resp.json()["image_urls"] == []
