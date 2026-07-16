"""BDD for the gacha-style candidate portrait flow.

The operator generates N candidate images, previews them, picks a
subset to commit as permanent portraits. Key invariants:

- ``generate_candidates`` writes to ``candidates/`` subdir, does NOT
  touch ``character.image_urls``.
- ``commit_candidates`` moves kept files to the main portrait dir,
  appends URLs to ``image_urls``, deletes unselected files.
- ``commit_candidates`` with empty keep & album lists = discard all.
- ``album_urls`` promotes candidates into the album (objects copied but
  not added to ``image_urls``; returned as ``CommittedAlbumCandidate``
  so the route can register them).
- Path-traversal in keep/album URLs is ignored silently (urls outside
  this character's candidates dir → no-op).
- ``MAX_IMAGES_PER_CHARACTER`` cap is respected at commit time (stage
  only; album has its own higher ceiling).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_image_service import (
    CharacterImageService,
    GenerationDisabledError,
    MAX_CANDIDATES_PER_BATCH,
    MAX_IMAGES_PER_CHARACTER,
    TooManyImagesError,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEMO_ACCOUNT_RUNTIME_PROFILE,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.tools.comfyui.generator import (
    ComfyPortraitGenerator,
)
from kokoro_link.infrastructure.tools.comfyui.workflow import (
    DEFAULT_WORKFLOW_FILE,
    WorkflowBuilder,
)


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _FakeClient:
    """Multi-image fake — returns N distinct bytes for one queue call,
    mimicking ComfyUI's ``batch_size=N`` behavior."""

    def __init__(self, num_images: int = 3) -> None:
        self.num_images = num_images
        self.queued_prompts: list[dict] = []
        self.last_batch_size: int | None = None

    async def queue_prompt(self, prompt: dict) -> str:
        self.queued_prompts.append(prompt)
        self.last_batch_size = prompt["5"]["inputs"]["batch_size"]
        return "pid-1"

    async def wait_for_completion(self, prompt_id: str) -> dict:
        return {
            "outputs": {
                "9": {
                    "images": [
                        {"filename": f"out_{i}.png", "subfolder": "", "type": "output"}
                        for i in range(self.num_images)
                    ],
                },
            },
        }

    async def download_image(
        self, *, filename: str, subfolder: str, folder_type: str,
    ) -> bytes:
        return _PNG_BYTES


class _StaticDemoRuntimeProfileResolver:
    async def resolve_for_operator(self, operator_id: str):
        return DEMO_ACCOUNT_RUNTIME_PROFILE


def _service(
    tmp_path: Path,
    *,
    num_images: int = 3,
    with_generator: bool = True,
    object_storage: InMemoryObjectStorage | None = None,
    account_runtime_profile_resolver=None,
) -> tuple[CharacterImageService, CharacterService, _FakeClient]:
    repo = InMemoryCharacterRepository()
    client = _FakeClient(num_images=num_images)
    generator = None
    if with_generator:
        generator = ComfyPortraitGenerator(
            client=client,  # type: ignore[arg-type]
            workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
        )
    from tests.unit._image_provider_stub import StaticActiveImageProvider
    active = StaticActiveImageProvider(generator) if generator is not None else None
    storage = object_storage or InMemoryObjectStorage(public_base_url="/uploads")
    service = CharacterImageService(
        character_repository=repo,
        uploads_dir=tmp_path,
        image_provider=active,
        object_storage=storage,
        account_runtime_profile_resolver=account_runtime_profile_resolver,
    )
    character_service = CharacterService(repo)
    return service, character_service, client


@pytest.mark.asyncio
async def test_generate_candidates_writes_to_candidate_objects(tmp_path: Path) -> None:
    service, chars, client = _service(tmp_path, num_images=3)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    character, urls = await service.generate_candidates(
        created.id, positive="cafe", count=3,
    )

    # image_urls untouched — candidates are pending.
    assert character.image_urls == ()
    assert len(urls) == 3
    storage = service._object_storage
    assert storage is not None
    for url in urls:
        assert "/candidates/" in url
        key = storage.object_key_from_url(url)
        assert key is not None
        assert await storage.stat(object_key=key) is not None
    # Batch_size was plumbed through to node 5.
    assert client.last_batch_size == 3


@pytest.mark.asyncio
async def test_demo_runtime_profile_disables_portrait_candidate_generation(
    tmp_path: Path,
) -> None:
    service, chars, client = _service(
        tmp_path,
        account_runtime_profile_resolver=_StaticDemoRuntimeProfileResolver(),
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    with pytest.raises(GenerationDisabledError):
        await service.generate_candidates(created.id, positive="cafe", count=3)
    with pytest.raises(GenerationDisabledError):
        await service.generate_portrait(created.id, positive="cafe")

    assert client.queued_prompts == []


@pytest.mark.asyncio
async def test_generate_candidates_count_clamped_to_max(tmp_path: Path) -> None:
    service, chars, client = _service(
        tmp_path, num_images=MAX_CANDIDATES_PER_BATCH,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    _, urls = await service.generate_candidates(
        created.id, positive="x", count=99,
    )

    assert len(urls) == MAX_CANDIDATES_PER_BATCH
    assert client.last_batch_size == MAX_CANDIDATES_PER_BATCH


@pytest.mark.asyncio
async def test_commit_moves_selected_and_deletes_rest(tmp_path: Path) -> None:
    service, chars, _ = _service(tmp_path, num_images=3)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    _, urls = await service.generate_candidates(
        created.id, positive="x", count=3,
    )
    # Keep the first two; discard the third.
    keep = urls[:2]
    discard = urls[2]
    storage = service._object_storage
    assert storage is not None
    discard_key = storage.object_key_from_url(discard)

    updated, album_entries = await service.commit_candidates(
        created.id, keep_urls=keep,
    )

    assert album_entries == []
    # Kept URLs appended (with the candidates/ path rewritten to main).
    assert len(updated.image_urls) == 2
    for permanent_url in updated.image_urls:
        assert "/candidates/" not in permanent_url
        key = storage.object_key_from_url(permanent_url)
        assert key is not None
        assert await storage.stat(object_key=key) is not None
    assert discard_key is not None
    assert await storage.stat(object_key=discard_key) is None


@pytest.mark.asyncio
async def test_commit_empty_keep_discards_all(tmp_path: Path) -> None:
    service, chars, _ = _service(tmp_path, num_images=3)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))
    _, urls = await service.generate_candidates(created.id, positive="x", count=3)
    storage = service._object_storage
    assert storage is not None
    keys = [storage.object_key_from_url(url) for url in urls]

    updated, album_entries = await service.commit_candidates(
        created.id, keep_urls=[],
    )

    assert updated.image_urls == ()
    assert album_entries == []
    for key in keys:
        assert key is not None
        assert await storage.stat(object_key=key) is None


@pytest.mark.asyncio
async def test_commit_ignores_urls_outside_candidates_dir(tmp_path: Path) -> None:
    """Security: passing a URL to the main image dir (or another
    character's dir) must NOT move or delete that file."""
    service, chars, _ = _service(tmp_path, num_images=2)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))
    # Seed a manual upload in the main dir — should survive a commit
    # that wildly points at it.
    existing = await service.add_image(
        created.id, data=_PNG_BYTES, mime_type="image/png",
        original_filename="manual.png",
    )
    manual_url = existing.image_urls[0]
    storage = service._object_storage
    assert storage is not None
    manual_key = storage.object_key_from_url(manual_url)
    assert manual_key is not None
    await service.generate_candidates(created.id, positive="x", count=2)

    # Commit with a "keep_urls" that lies about the manual portrait —
    # it shouldn't be selected, moved, or deleted.
    updated, _ = await service.commit_candidates(
        created.id, keep_urls=[manual_url],
    )

    # Manual upload still present and unchanged.
    assert manual_url in updated.image_urls
    assert await storage.stat(object_key=manual_key) is not None


@pytest.mark.asyncio
async def test_commit_respects_image_cap(tmp_path: Path) -> None:
    """When headroom is less than the keep list, extras are discarded
    rather than pushing past ``MAX_IMAGES_PER_CHARACTER``."""
    service, chars, _ = _service(tmp_path, num_images=3)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))
    # Fill up to one-below-cap with manual uploads.
    for _ in range(MAX_IMAGES_PER_CHARACTER - 1):
        await service.add_image(
            created.id, data=_PNG_BYTES, mime_type="image/png",
            original_filename="x.png",
        )
    _, urls = await service.generate_candidates(
        created.id, positive="x", count=3,
    )

    updated, _ = await service.commit_candidates(
        created.id, keep_urls=urls,
    )

    # Only one extra candidate fit; the other two should be deleted.
    assert len(updated.image_urls) == MAX_IMAGES_PER_CHARACTER


@pytest.mark.asyncio
async def test_generate_candidates_requires_generator(tmp_path: Path) -> None:
    service, chars, _ = _service(tmp_path, with_generator=False)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    with pytest.raises(GenerationDisabledError):
        await service.generate_candidates(created.id, positive="x", count=3)


@pytest.mark.asyncio
async def test_generate_candidates_blocked_when_at_cap(tmp_path: Path) -> None:
    service, chars, _ = _service(tmp_path, num_images=3)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))
    for _ in range(MAX_IMAGES_PER_CHARACTER):
        await service.add_image(
            created.id, data=_PNG_BYTES, mime_type="image/png",
            original_filename="x.png",
        )

    with pytest.raises(TooManyImagesError):
        await service.generate_candidates(created.id, positive="x", count=3)


@pytest.mark.asyncio
async def test_commit_no_candidates_returns_character_unchanged(
    tmp_path: Path,
) -> None:
    service, chars, _ = _service(tmp_path, with_generator=False)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    updated, album_entries = await service.commit_candidates(
        created.id, keep_urls=[],
    )

    assert updated.image_urls == ()
    assert album_entries == []


@pytest.mark.asyncio
async def test_commit_album_urls_moves_objects_without_touching_stage(
    tmp_path: Path,
) -> None:
    """album_urls picks are moved out of candidates/ but NOT appended to
    Character.image_urls — the route layer turns each into an AlbumItem."""
    service, chars, _ = _service(tmp_path, num_images=3)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    _, urls = await service.generate_candidates(
        created.id, positive="x", count=3,
    )
    stage_url = urls[0]
    album_url = urls[1]
    # urls[2] is neither → discarded.

    updated, album_entries = await service.commit_candidates(
        created.id, keep_urls=[stage_url], album_urls=[album_url],
    )

    # Only the stage pick is on image_urls.
    assert len(updated.image_urls) == 1
    assert "/candidates/" not in updated.image_urls[0]
    # The album pick came back for the route to register.
    assert len(album_entries) == 1
    entry = album_entries[0]
    assert "/candidates/" not in entry.url
    storage = service._object_storage
    assert storage is not None
    entry_key = storage.object_key_from_url(entry.url)
    assert entry_key is not None
    assert await storage.stat(object_key=entry_key) is not None
    # byte_size was captured post-rename.
    assert entry.byte_size is not None and entry.byte_size > 0
    discarded_key = storage.object_key_from_url(urls[2])
    assert discarded_key is not None
    assert await storage.stat(object_key=discarded_key) is None


@pytest.mark.asyncio
async def test_commit_album_only_leaves_stage_untouched(tmp_path: Path) -> None:
    """album_urls alone shouldn't touch character.image_urls at all."""
    service, chars, _ = _service(tmp_path, num_images=2)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))
    _, urls = await service.generate_candidates(
        created.id, positive="x", count=2,
    )

    updated, album_entries = await service.commit_candidates(
        created.id, keep_urls=[], album_urls=list(urls),
    )

    assert updated.image_urls == ()
    assert len(album_entries) == 2


@pytest.mark.asyncio
async def test_commit_url_in_both_lists_goes_to_stage(tmp_path: Path) -> None:
    """A URL appearing in both keep_urls and album_urls is treated as a
    stage pick — ensures the operator's stage intent isn't silently
    demoted to album."""
    service, chars, _ = _service(tmp_path, num_images=1)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))
    _, urls = await service.generate_candidates(
        created.id, positive="x", count=1,
    )

    updated, album_entries = await service.commit_candidates(
        created.id, keep_urls=list(urls), album_urls=list(urls),
    )

    assert len(updated.image_urls) == 1
    assert album_entries == []


@pytest.mark.asyncio
async def test_commit_album_ignores_urls_outside_candidates_dir(
    tmp_path: Path,
) -> None:
    """Passing a non-candidate URL in album_urls must not move or
    register anything — same security stance as keep_urls."""
    service, chars, _ = _service(tmp_path, num_images=1)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))
    existing = await service.add_image(
        created.id, data=_PNG_BYTES, mime_type="image/png",
        original_filename="manual.png",
    )
    manual_url = existing.image_urls[0]
    storage = service._object_storage
    assert storage is not None
    manual_key = storage.object_key_from_url(manual_url)
    assert manual_key is not None
    await service.generate_candidates(created.id, positive="x", count=1)

    updated, album_entries = await service.commit_candidates(
        created.id, keep_urls=[], album_urls=[manual_url],
    )

    # Manual portrait still in place and untouched.
    assert manual_url in updated.image_urls
    assert album_entries == []
    assert await storage.stat(object_key=manual_key) is not None


@pytest.mark.asyncio
async def test_storage_generate_candidates_writes_objects_not_files(
    tmp_path: Path,
) -> None:
    storage = InMemoryObjectStorage(public_base_url="/media")
    service, chars, client = _service(
        tmp_path, num_images=2, object_storage=storage,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    character, urls = await service.generate_candidates(
        created.id, positive="cafe", count=2,
    )

    assert character.image_urls == ()
    assert len(urls) == 2
    assert client.last_batch_size == 2
    assert not (tmp_path / "characters" / created.id / "candidates").exists()
    for url in urls:
        key = storage.object_key_from_url(url)
        assert key is not None
        assert key.startswith(f"characters/{created.id}/candidates/")
        assert await storage.stat(object_key=key) is not None


@pytest.mark.asyncio
async def test_storage_commit_copies_selected_and_deletes_candidates(
    tmp_path: Path,
) -> None:
    storage = InMemoryObjectStorage(public_base_url="/media")
    service, chars, _ = _service(
        tmp_path, num_images=3, object_storage=storage,
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))
    _, urls = await service.generate_candidates(
        created.id, positive="x", count=3,
    )
    source_keys = [
        storage.object_key_from_url(url)
        for url in urls
    ]

    updated, album_entries = await service.commit_candidates(
        created.id,
        keep_urls=[urls[0]],
        album_urls=[urls[1]],
    )

    assert len(updated.image_urls) == 1
    assert "/candidates/" not in updated.image_urls[0]
    assert len(album_entries) == 1
    assert "/candidates/" not in album_entries[0].url
    assert album_entries[0].byte_size == len(_PNG_BYTES)

    for key in source_keys:
        assert key is not None
        assert await storage.stat(object_key=key) is None

    stage_key = storage.object_key_from_url(updated.image_urls[0])
    album_key = storage.object_key_from_url(album_entries[0].url)
    assert stage_key is not None
    assert album_key is not None
    assert await storage.get_bytes(object_key=stage_key) == _PNG_BYTES
    assert await storage.get_bytes(object_key=album_key) == _PNG_BYTES
