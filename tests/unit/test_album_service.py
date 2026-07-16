"""BDD for ``AlbumService``.

Covers the glue between ``AlbumRepositoryPort``, ``CharacterRepositoryPort``,
and Object Storage. Tool-side integration (``ComfyImageTool`` calling
``add_auto``) is exercised in ``test_comfy_tool_album.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.services.album_service import (
    MAX_ALBUM_ITEMS_PER_CHARACTER,
    AlbumItemNotFoundError,
    AlbumService,
    StageFullError,
    StageImageNotFoundError,
)
from kokoro_link.domain.entities.album_item import SOURCE_STAGE, SOURCE_TOOL
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_album import (
    InMemoryAlbumRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage


@pytest.fixture
def uploads(tmp_path: Path) -> Path:
    return tmp_path / "uploads"


@pytest.fixture
def character_repo() -> InMemoryCharacterRepository:
    return InMemoryCharacterRepository()


@pytest.fixture
def album_repo() -> InMemoryAlbumRepository:
    return InMemoryAlbumRepository()


@pytest.fixture
def service(
    album_repo: InMemoryAlbumRepository,
    character_repo: InMemoryCharacterRepository,
    uploads: Path,
) -> AlbumService:
    uploads.mkdir(parents=True, exist_ok=True)
    return AlbumService(
        album_repository=album_repo,
        character_repository=character_repo,
        uploads_dir=uploads,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
    )


async def _seed_character(
    repo: InMemoryCharacterRepository, name: str = "Alice",
) -> Character:
    character = Character.create(
        name=name,
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    await repo.save(character)
    return character


def _write_file(uploads: Path, character_id: str, relative: str) -> Path:
    """Create a dummy file under ``uploads/characters/{id}/{relative}``."""
    target = uploads / "characters" / character_id / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
    return target


# ---------- add_auto ----------


@pytest.mark.asyncio
async def test_add_auto_stores_tool_source(
    service: AlbumService,
    character_repo: InMemoryCharacterRepository,
) -> None:
    character = await _seed_character(character_repo)
    item = await service.add_auto(
        character_id=character.id,
        url=f"/uploads/characters/{character.id}/tools/gen.png",
        caption="午後陽光",
        byte_size=12345,
    )
    assert item.source == SOURCE_TOOL
    assert item.caption == "午後陽光"
    assert item.byte_size == 12345
    assert len(await service.list_for_character(character.id)) == 1


@pytest.mark.asyncio
async def test_add_auto_gcs_oldest_when_over_cap(
    service: AlbumService,
    album_repo: InMemoryAlbumRepository,
    character_repo: InMemoryCharacterRepository,
    monkeypatch: pytest.MonkeyPatch,
    uploads: Path,
) -> None:
    """Tool path mustn't raise when album is full — silently drop
    oldest instead. Proves the policy with a tiny cap."""
    monkeypatch.setattr(
        "kokoro_link.application.services.album_service.MAX_ALBUM_ITEMS_PER_CHARACTER",
        3,
    )
    character = await _seed_character(character_repo)

    storage = service._object_storage
    assert storage is not None
    # Seed the cap + one object per entry so GC has something to delete
    first = await storage.put_bytes(
        object_key=f"characters/{character.id}/tools/first.png",
        content=b"\x89PNG\r\n\x1a\nFAKE",
        content_type="image/png",
    )
    for idx, name in enumerate(["first.png", "second.png", "third.png"]):
        await service.add_auto(
            character_id=character.id,
            url=f"/uploads/characters/{character.id}/tools/{name}",
        )

    # Fourth insertion should kick the oldest ("first.png")
    await service.add_auto(
        character_id=character.id,
        url=f"/uploads/characters/{character.id}/tools/fourth.png",
    )

    remaining = await service.list_for_character(character.id)
    urls = [it.url.rsplit("/", 1)[-1] for it in remaining]
    assert "first.png" not in urls
    assert len(remaining) == 3
    assert await storage.stat(object_key=first.object_key) is None


# ---------- delete ----------


@pytest.mark.asyncio
async def test_delete_removes_row_and_object(
    service: AlbumService,
    character_repo: InMemoryCharacterRepository,
    uploads: Path,
) -> None:
    character = await _seed_character(character_repo)
    storage = service._object_storage
    assert storage is not None
    stored = await storage.put_bytes(
        object_key=f"characters/{character.id}/tools/abc.png",
        content=b"\x89PNG\r\n\x1a\nFAKE",
        content_type="image/png",
    )
    item = await service.add_auto(
        character_id=character.id,
        url=stored.url,
    )

    await service.delete(item.id)

    assert await service.list_for_character(character.id) == []
    assert await storage.stat(object_key=stored.object_key) is None


@pytest.mark.asyncio
async def test_delete_raises_on_missing(service: AlbumService) -> None:
    with pytest.raises(AlbumItemNotFoundError):
        await service.delete("nope")


@pytest.mark.asyncio
async def test_delete_file_missing_is_tolerated(
    service: AlbumService,
    character_repo: InMemoryCharacterRepository,
) -> None:
    """DB row deletes even if the object was already lost — keeps DB and
    UI state consistent."""
    character = await _seed_character(character_repo)
    item = await service.add_auto(
        character_id=character.id,
        url=f"/uploads/characters/{character.id}/tools/ghost.png",
    )
    # File never existed — delete should still succeed
    await service.delete(item.id)
    assert await service.list_for_character(character.id) == []


# ---------- transfer_from_stage ----------


@pytest.mark.asyncio
async def test_transfer_from_stage_moves_index_not_object(
    service: AlbumService,
    character_repo: InMemoryCharacterRepository,
    uploads: Path,
) -> None:
    character = await _seed_character(character_repo)
    storage = service._object_storage
    assert storage is not None
    stored = await storage.put_bytes(
        object_key=f"characters/{character.id}/stage.png",
        content=b"\x89PNG\r\n\x1a\nFAKE",
        content_type="image/png",
    )
    stage_url = stored.url
    # Attach the URL to the character's image list
    character = character.with_image_urls((stage_url,))
    await character_repo.save(character)

    updated, item = await service.transfer_from_stage(
        character_id=character.id, url=stage_url,
    )

    # Stage lost the URL; album gained a row; object still exists.
    assert updated.image_urls == ()
    assert item.source == SOURCE_STAGE
    assert item.url == stage_url
    assert await storage.stat(object_key=stored.object_key) is not None


@pytest.mark.asyncio
async def test_transfer_url_not_on_stage_raises(
    service: AlbumService,
    character_repo: InMemoryCharacterRepository,
) -> None:
    character = await _seed_character(character_repo)
    with pytest.raises(StageImageNotFoundError):
        await service.transfer_from_stage(
            character_id=character.id,
            url=f"/uploads/characters/{character.id}/ghost.png",
        )


# ---------- promote_to_stage ----------


@pytest.mark.asyncio
async def test_promote_moves_item_back_to_stage(
    service: AlbumService,
    character_repo: InMemoryCharacterRepository,
) -> None:
    character = await _seed_character(character_repo)
    url = f"/uploads/characters/{character.id}/tools/hero.png"
    item = await service.add_auto(character_id=character.id, url=url)

    updated = await service.promote_to_stage(item.id)

    assert updated.image_urls == (url,)
    assert await service.list_for_character(character.id) == []


@pytest.mark.asyncio
async def test_promote_rejects_when_stage_full(
    service: AlbumService,
    character_repo: InMemoryCharacterRepository,
) -> None:
    character = await _seed_character(character_repo)
    # Fill the stage to the 12-slot cap
    full_urls = tuple(
        f"/uploads/characters/{character.id}/stage_{i}.png"
        for i in range(12)
    )
    character = character.with_image_urls(full_urls)
    await character_repo.save(character)

    item = await service.add_auto(
        character_id=character.id,
        url=f"/uploads/characters/{character.id}/tools/new.png",
    )
    with pytest.raises(StageFullError):
        await service.promote_to_stage(item.id)


@pytest.mark.asyncio
async def test_promote_unknown_item_raises(service: AlbumService) -> None:
    with pytest.raises(AlbumItemNotFoundError):
        await service.promote_to_stage("no-such")


@pytest.mark.asyncio
async def test_list_is_newest_first(
    service: AlbumService,
    character_repo: InMemoryCharacterRepository,
) -> None:
    character = await _seed_character(character_repo)
    for idx in range(3):
        await service.add_auto(
            character_id=character.id,
            url=f"/uploads/characters/{character.id}/tools/{idx}.png",
        )
    items = await service.list_for_character(character.id)
    assert [i.url.rsplit("/", 1)[-1] for i in items] == [
        "2.png", "1.png", "0.png",
    ]


# ---------- storage safety ----------


@pytest.mark.asyncio
async def test_delete_refuses_path_traversal(
    service: AlbumService,
    album_repo: InMemoryAlbumRepository,
    character_repo: InMemoryCharacterRepository,
    uploads: Path,
) -> None:
    """URL that escapes the character root (e.g. ``..``) must NOT be
    turned into an object delete — only the album row is touched."""
    character = await _seed_character(character_repo)
    storage = service._object_storage
    assert storage is not None
    victim = await storage.put_bytes(
        object_key="characters/other-char-secret.png",
        content=b"important",
        content_type="image/png",
    )

    # Sneak a malicious URL into the repo directly (bypasses service.add_auto
    # so we can test the defensive resolver in isolation)
    from kokoro_link.domain.entities.album_item import AlbumItem
    bad = AlbumItem.create(
        character_id=character.id,
        url=f"/uploads/characters/{character.id}/../other-char-secret.png",
        source=SOURCE_TOOL,
    )
    await album_repo.add(bad)

    await service.delete(bad.id)
    assert await storage.stat(object_key=victim.object_key) is not None


@pytest.mark.asyncio
async def test_delete_removes_object_storage_item(
    album_repo: InMemoryAlbumRepository,
    character_repo: InMemoryCharacterRepository,
    uploads: Path,
) -> None:
    character = await _seed_character(character_repo)
    storage = InMemoryObjectStorage(public_base_url="/media")
    stored = await storage.put_bytes(
        object_key=f"characters/{character.id}/tools/abc.png",
        content=b"\x89PNG\r\n\x1a\nFAKE",
        content_type="image/png",
        metadata={"character_id": character.id, "kind": "tool"},
    )
    service = AlbumService(
        album_repository=album_repo,
        character_repository=character_repo,
        uploads_dir=uploads,
        object_storage=storage,
    )
    item = await service.add_auto(
        character_id=character.id,
        url=stored.url,
        byte_size=stored.size_bytes,
    )

    await service.delete(item.id)

    assert await service.list_for_character(character.id) == []
    assert await storage.stat(object_key=stored.object_key) is None
