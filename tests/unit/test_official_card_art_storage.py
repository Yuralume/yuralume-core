"""Official card art in a character's gallery — landing it, and keeping it.

Two halves of one rule (EC4): an IP partner's art lands under keys that say
whose it is, and those keys are what the delete path reads back. The player
keeps full control of everything they added themselves.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.services.character_image_service import (
    CharacterImageService,
    OfficialArtProtectedError,
    OfficialCardArtImage,
)
from kokoro_link.application.services.official_character_art import (
    select_official_reference_urls,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage

_PLAYER = "player-1"
_CARD_ID = "mio-cafe"


async def _managed_character(repo: InMemoryCharacterRepository) -> Character:
    character = Character.create(
        name="美緒",
        summary="咖啡廳打工女大生",
        user_id=_PLAYER,
        personality=[],
        interests=[],
        speaking_style="natural",
        boundaries=[],
        state=CharacterState(
            emotion="calm", affection=0, fatigue=0, trust=0, energy=100,
        ),
        origin_official_card_id=_CARD_ID,
    )
    await repo.save(character)
    return character


def _service(repo, storage) -> CharacterImageService:
    return CharacterImageService(
        character_repository=repo,
        uploads_dir=Path("."),
        object_storage=storage,
    )


@pytest.mark.asyncio
async def test_official_art_lands_in_carousel_order_under_marked_keys() -> None:
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _managed_character(repo)

    updated = await _service(repo, storage).attach_official_card_art(
        character.id,
        images=[
            OfficialCardArtImage(index=0, data=b"a", content_type="image/png"),
            OfficialCardArtImage(
                index=1, data=b"b", content_type="image/jpeg", reference=True,
            ),
        ],
    )

    assert [storage.object_key_from_url(u) for u in updated.image_urls] == [
        f"characters/{character.id}/official/0.png",
        f"characters/{character.id}/official-reference/1.jpg",
    ]


@pytest.mark.asyncio
async def test_landing_the_same_card_twice_overwrites_rather_than_piles_up(
) -> None:
    # Deterministic keys are the point of not reusing the upload path: a
    # repeated install must not leave a second copy of every portrait
    # orphaned in storage.
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _managed_character(repo)
    service = _service(repo, storage)
    art = [OfficialCardArtImage(index=0, data=b"a", content_type="image/png")]

    await service.attach_official_card_art(character.id, images=art)
    updated = await service.attach_official_card_art(character.id, images=art)

    assert len(set(updated.image_urls)) == 1


@pytest.mark.asyncio
async def test_an_unsupported_image_type_is_skipped_not_fatal() -> None:
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _managed_character(repo)

    updated = await _service(repo, storage).attach_official_card_art(
        character.id,
        images=[
            OfficialCardArtImage(
                index=0, data=b"a", content_type="application/pdf",
            ),
            OfficialCardArtImage(index=1, data=b"b", content_type="image/png"),
        ],
    )

    assert len(updated.image_urls) == 1
    assert updated.image_urls[0].endswith("/official/1.png")


@pytest.mark.asyncio
async def test_the_player_cannot_delete_the_partners_art() -> None:
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _managed_character(repo)
    service = _service(repo, storage)
    updated = await service.attach_official_card_art(
        character.id,
        images=[
            OfficialCardArtImage(index=0, data=b"a", content_type="image/png"),
        ],
    )
    official_url = updated.image_urls[0]

    with pytest.raises(OfficialArtProtectedError):
        await service.remove_image(character.id, url=official_url)

    kept = await repo.get(character.id)
    assert kept is not None
    assert official_url in kept.image_urls
    # And the object itself is still there — a refused delete must not have
    # got halfway.
    key = storage.object_key_from_url(official_url)
    assert key is not None
    assert await storage.get_bytes(object_key=key) == b"a"


@pytest.mark.asyncio
async def test_the_player_still_deletes_their_own_art_on_a_managed_character(
) -> None:
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _managed_character(repo)
    service = _service(repo, storage)
    await service.attach_official_card_art(
        character.id,
        images=[
            OfficialCardArtImage(index=0, data=b"a", content_type="image/png"),
        ],
    )
    with_upload = await service.add_image(
        character.id, data=b"mine", mime_type="image/png",
    )
    own_url = with_upload.image_urls[-1]

    updated = await service.remove_image(character.id, url=own_url)

    assert own_url not in updated.image_urls
    assert len(updated.image_urls) == 1


@pytest.mark.asyncio
async def test_ec6_reads_back_only_the_marked_reference_art() -> None:
    # The seam EC6 consumes: which of a managed character's images are the
    # likeness lock, asked once here rather than re-derived per call site.
    repo = InMemoryCharacterRepository()
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    character = await _managed_character(repo)
    service = _service(repo, storage)
    updated = await service.attach_official_card_art(
        character.id,
        images=[
            OfficialCardArtImage(index=0, data=b"a", content_type="image/png"),
            OfficialCardArtImage(
                index=1, data=b"b", content_type="image/png", reference=True,
            ),
        ],
    )
    with_upload = await service.add_image(
        character.id, data=b"mine", mime_type="image/png",
    )

    assert select_official_reference_urls(
        with_upload.image_urls, storage.object_key_from_url,
    ) == [updated.image_urls[1]]
