"""BDD: album candidate batches must survive a cross-instance hop.

Hosted runs behind ``core-router`` load-balancing 2× api replicas, so the
``generate`` request and the ``commit`` request for the same gacha batch
routinely land on *different* processes. The batch bookkeeping used to be
a per-process ``dict``, which made that split silently destructive:

- generate on A, commit on B → B saw an empty batch and returned
  ``(character, [])``: every image the player picked was dropped.
- the next generate on B could not see A's keys → the previous batch's
  objects leaked in object storage forever.
- a restart evaporated every in-flight batch.

The batch list now lives in a shared repository, so these tests simulate
the deployment directly: two ``CharacterImageService`` instances (= two
api processes) sharing one repository and one object store.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_image_service import (
    CharacterImageService,
    StorageUnavailableError,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.contracts.object_storage import ObjectStorageUnavailableError
from kokoro_link.infrastructure.repositories.in_memory_character_image_candidate_batches import (  # noqa: E501
    InMemoryCharacterImageCandidateBatchRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from tests.unit._image_provider_stub import StaticActiveImageProvider


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_CANDIDATE_MARKER = "/candidates/"


class _GeneratingProvider:
    provider_id = "stub-image"

    def __init__(self, outputs: list[bytes]) -> None:
        self.outputs = outputs

    async def generate(self, **kwargs):  # noqa: ANN003
        return list(self.outputs)


class _CopyUnreachableObjectStorage(InMemoryObjectStorage):
    """Storage that accepts writes but fails the commit-time copy."""

    async def copy(self, **kwargs):  # noqa: ANN003
        raise ObjectStorageUnavailableError("object storage unreachable")


class _CopyBrokenObjectStorage(InMemoryObjectStorage):
    """Storage whose copy fails in a way that is *not* worth retrying."""

    async def copy(self, **kwargs):  # noqa: ANN003
        raise RuntimeError("copy exploded")


class _PutUnreachableObjectStorage(InMemoryObjectStorage):
    """Storage that goes down between the batch bookkeeping and the writes."""

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        super().__init__(**kwargs)
        self.accept_puts = True

    async def put_bytes(self, **kwargs):  # noqa: ANN003
        if not self.accept_puts:
            raise ObjectStorageUnavailableError("object storage unreachable")
        return await super().put_bytes(**kwargs)


class _RacingBatchRepository(InMemoryCharacterImageCandidateBatchRepository):
    """Fires an armed hook right after a ``get`` returns.

    That is the exact window the compare-and-swap exists for: this replica has
    read the batch keys it is about to act on, and another replica supersedes
    the row before the first replica gets to write its decision back.
    """

    def __init__(self) -> None:
        super().__init__()
        self._armed = None

    def arm(self, hook) -> None:  # noqa: ANN001
        self._armed = hook

    async def get(self, character_id: str):  # noqa: ANN201
        keys = await super().get(character_id)
        hook, self._armed = self._armed, None
        if hook is not None:
            await hook()
        return keys


def _make_instance(
    *,
    characters: InMemoryCharacterRepository,
    storage: InMemoryObjectStorage,
    batches: InMemoryCharacterImageCandidateBatchRepository,
    outputs: list[bytes],
    uploads_dir: Path,
) -> CharacterImageService:
    """One api replica: its own service object, shared backing stores."""
    return CharacterImageService(
        character_repository=characters,
        uploads_dir=uploads_dir,
        object_storage=storage,
        image_provider=StaticActiveImageProvider(_GeneratingProvider(outputs)),
        candidate_batch_repository=batches,
    )


def _candidate_keys(storage: InMemoryObjectStorage) -> set[str]:
    return {key for key in storage._objects if _CANDIDATE_MARKER in key}


async def _seed_character(characters: InMemoryCharacterRepository) -> str:
    created = await CharacterService(characters).create_character(
        CreateCharacterRequest(name="Mio"),
    )
    return created.id


@pytest.mark.asyncio
async def test_commit_on_a_second_instance_moves_the_selected_candidate(
    tmp_path: Path,
) -> None:
    """Instance A generates, the load balancer sends commit to instance B."""
    characters = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    batches = InMemoryCharacterImageCandidateBatchRepository()
    instance_a = _make_instance(
        characters=characters, storage=storage, batches=batches,
        outputs=[_PNG_BYTES, _PNG_BYTES], uploads_dir=tmp_path,
    )
    instance_b = _make_instance(
        characters=characters, storage=storage, batches=batches,
        outputs=[_PNG_BYTES, _PNG_BYTES], uploads_dir=tmp_path,
    )
    character_id = await _seed_character(characters)

    _, urls = await instance_a.generate_candidates(
        character_id, positive="portrait", count=2,
    )
    assert len(urls) == 2

    updated, album_entries = await instance_b.commit_candidates(
        character_id, keep_urls=[urls[0]], album_urls=[urls[1]],
    )

    assert len(updated.image_urls) == 1, "kept candidate must reach the stage"
    assert _CANDIDATE_MARKER not in updated.image_urls[0]
    assert len(album_entries) == 1
    # Every candidate object has been consumed — none left behind.
    assert _candidate_keys(storage) == set()
    assert await batches.get(character_id) == ()


@pytest.mark.asyncio
async def test_generate_on_a_second_instance_purges_the_previous_batch(
    tmp_path: Path,
) -> None:
    """Instance B must see (and clean up) the batch instance A wrote."""
    characters = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    batches = InMemoryCharacterImageCandidateBatchRepository()
    instance_a = _make_instance(
        characters=characters, storage=storage, batches=batches,
        outputs=[_PNG_BYTES, _PNG_BYTES], uploads_dir=tmp_path,
    )
    instance_b = _make_instance(
        characters=characters, storage=storage, batches=batches,
        outputs=[_PNG_BYTES], uploads_dir=tmp_path,
    )
    character_id = await _seed_character(characters)

    _, first_urls = await instance_a.generate_candidates(
        character_id, positive="portrait", count=2,
    )
    stale_keys = _candidate_keys(storage)
    assert len(stale_keys) == 2

    _, second_urls = await instance_b.generate_candidates(
        character_id, positive="portrait", count=1,
    )

    live_keys = _candidate_keys(storage)
    assert stale_keys & live_keys == set(), "stale candidates must be deleted"
    assert len(live_keys) == 1
    assert set(await batches.get(character_id)) == live_keys
    assert first_urls != second_urls


@pytest.mark.asyncio
async def test_commit_without_a_stored_batch_is_a_no_op(tmp_path: Path) -> None:
    """No batch row (already committed / expired) keeps the old contract."""
    characters = InMemoryCharacterRepository()
    service = _make_instance(
        characters=characters,
        storage=InMemoryObjectStorage(public_base_url="/uploads"),
        batches=InMemoryCharacterImageCandidateBatchRepository(),
        outputs=[_PNG_BYTES],
        uploads_dir=tmp_path,
    )
    character_id = await _seed_character(characters)

    character, album_entries = await service.commit_candidates(
        character_id, keep_urls=["/uploads/whatever.png"],
    )

    assert character.image_urls == ()
    assert album_entries == []


@pytest.mark.asyncio
async def test_storage_outage_leaves_the_batch_for_a_cross_instance_retry(
    tmp_path: Path,
) -> None:
    """The 503 tells the player to retry — possibly on the *other* replica,
    so the surviving keys must be in the shared repository, not in the
    process that happened to serve the failed attempt."""
    characters = InMemoryCharacterRepository()
    storage = _CopyUnreachableObjectStorage(public_base_url="/uploads")
    batches = InMemoryCharacterImageCandidateBatchRepository()
    instance_a = _make_instance(
        characters=characters, storage=storage, batches=batches,
        outputs=[_PNG_BYTES, _PNG_BYTES], uploads_dir=tmp_path,
    )
    instance_b = _make_instance(
        characters=characters, storage=storage, batches=batches,
        outputs=[_PNG_BYTES, _PNG_BYTES], uploads_dir=tmp_path,
    )
    character_id = await _seed_character(characters)
    _, urls = await instance_a.generate_candidates(
        character_id, positive="portrait", count=2,
    )

    with pytest.raises(StorageUnavailableError):
        await instance_b.commit_candidates(character_id, keep_urls=[urls[0]])

    remaining = await batches.get(character_id)
    assert len(remaining) == 2, "uncommitted candidates must survive the outage"


# --- a stale replica must never clobber a newer batch ---------------------
#
# Sharing the batch across replicas fixed the *lost* batch; it opened a second
# race in exchange. Replica A reads the batch keys at the top of a commit (or a
# generate) and writes its verdict back at the bottom; replica B can install a
# brand-new batch in between. An unconditional write from A then deletes or
# overwrites B's row, and B's freshly generated candidates become orphans the
# player can see but never commit. Every write derived from a *previously read*
# batch is therefore conditional on that batch still being the live one.


_NEWER_BATCH_KEY_SUFFIX = "candidates/2b2b2b2b/newer.png"


async def _install_newer_batch(
    batches: InMemoryCharacterImageCandidateBatchRepository,
    character_id: str,
) -> tuple[str, ...]:
    """Stand in for another replica's generate finishing right now."""
    keys = (f"characters/{character_id}/{_NEWER_BATCH_KEY_SUFFIX}",)
    await InMemoryCharacterImageCandidateBatchRepository.replace(
        batches, character_id, keys,
    )
    return keys


@pytest.mark.asyncio
async def test_commit_does_not_delete_a_batch_generated_after_it_read(
    tmp_path: Path,
) -> None:
    """Instance A's closing clear must not take instance B's new batch."""
    characters = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    batches = _RacingBatchRepository()
    instance_a = _make_instance(
        characters=characters, storage=storage, batches=batches,
        outputs=[_PNG_BYTES, _PNG_BYTES], uploads_dir=tmp_path,
    )
    character_id = await _seed_character(characters)
    _, urls = await instance_a.generate_candidates(
        character_id, positive="portrait", count=2,
    )

    newer_keys = ()

    async def supersede() -> None:
        nonlocal newer_keys
        newer_keys = await _install_newer_batch(batches, character_id)

    batches.arm(supersede)
    updated, _ = await instance_a.commit_candidates(
        character_id, keep_urls=[urls[0]],
    )

    assert len(updated.image_urls) == 1, "the commit itself still succeeds"
    assert await batches.get(character_id) == newer_keys, (
        "the newer batch must stay committable"
    )


@pytest.mark.asyncio
async def test_storage_outage_does_not_overwrite_a_newer_batch(
    tmp_path: Path,
) -> None:
    """The outage retry path writes the *remaining* keys — but only if the
    batch it is shrinking is still the live one."""
    characters = InMemoryCharacterRepository()
    storage = _CopyUnreachableObjectStorage(public_base_url="/uploads")
    batches = _RacingBatchRepository()
    service = _make_instance(
        characters=characters, storage=storage, batches=batches,
        outputs=[_PNG_BYTES, _PNG_BYTES], uploads_dir=tmp_path,
    )
    character_id = await _seed_character(characters)
    _, urls = await service.generate_candidates(
        character_id, positive="portrait", count=2,
    )

    newer_keys = ()

    async def supersede() -> None:
        nonlocal newer_keys
        newer_keys = await _install_newer_batch(batches, character_id)

    batches.arm(supersede)
    with pytest.raises(StorageUnavailableError):
        await service.commit_candidates(character_id, keep_urls=[urls[0]])

    assert await batches.get(character_id) == newer_keys, (
        "a stale remaining-set must not overwrite the newer batch"
    )


@pytest.mark.asyncio
async def test_non_retryable_failure_does_not_delete_a_newer_batch(
    tmp_path: Path,
) -> None:
    characters = InMemoryCharacterRepository()
    storage = _CopyBrokenObjectStorage(public_base_url="/uploads")
    batches = _RacingBatchRepository()
    service = _make_instance(
        characters=characters, storage=storage, batches=batches,
        outputs=[_PNG_BYTES], uploads_dir=tmp_path,
    )
    character_id = await _seed_character(characters)
    _, urls = await service.generate_candidates(
        character_id, positive="portrait", count=1,
    )

    newer_keys = ()

    async def supersede() -> None:
        nonlocal newer_keys
        newer_keys = await _install_newer_batch(batches, character_id)

    batches.arm(supersede)
    with pytest.raises(RuntimeError):
        await service.commit_candidates(character_id, keep_urls=[urls[0]])

    assert await batches.get(character_id) == newer_keys


@pytest.mark.asyncio
async def test_generate_does_not_purge_a_batch_it_never_read(
    tmp_path: Path,
) -> None:
    """The purge at the top of generate is conditional too.

    It reads the previous batch, drops the row, then deletes those objects. If
    another replica supersedes the row in between, this generate owns neither
    the row nor the objects any more — and it may itself abort before writing a
    replacement, which would leave the character with no batch at all.
    """
    characters = InMemoryCharacterRepository()
    storage = _PutUnreachableObjectStorage(public_base_url="/uploads")
    batches = _RacingBatchRepository()
    service = _make_instance(
        characters=characters, storage=storage, batches=batches,
        outputs=[_PNG_BYTES], uploads_dir=tmp_path,
    )
    character_id = await _seed_character(characters)
    await service.generate_candidates(character_id, positive="portrait", count=1)
    previous_objects = _candidate_keys(storage)
    assert len(previous_objects) == 1

    newer_keys = ()

    async def supersede() -> None:
        nonlocal newer_keys
        newer_keys = await _install_newer_batch(batches, character_id)

    batches.arm(supersede)
    storage.accept_puts = False
    with pytest.raises(StorageUnavailableError):
        await service.generate_candidates(
            character_id, positive="portrait", count=1,
        )

    assert await batches.get(character_id) == newer_keys
    # Objects belonging to a batch we no longer own are somebody else's to
    # clean up — deleting them could yank the source out of an in-flight copy.
    assert _candidate_keys(storage) >= previous_objects
