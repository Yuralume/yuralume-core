"""Character album service — CRUD + stage transfer.

Four operations wrap the ``AlbumRepositoryPort`` so routes and the tool
don't have to coordinate DB + filesystem + character entity themselves:

- ``add_auto`` — called from ``ComfyImageTool`` after a successful
  generation; inserts an album row pointing at the file the tool just
  wrote. GC oldest when over the sanity cap (tool paths mustn't raise
  just because the album is full).
    - ``delete`` — operator UI action; removes row + object.
- ``transfer_from_stage`` — move a ``Character.image_urls`` entry into
  the album (index flips, file stays put).
- ``promote_to_stage`` — reverse: move an album entry into the stage
  carousel, honouring its 12-slot cap.

Object deletion is best-effort; losing the bytes but keeping the DB
consistent is strictly better than the reverse (album row points at
nothing vs orphan file wastes disk).
"""

from __future__ import annotations

import logging
from pathlib import Path

from kokoro_link.contracts.album import AlbumRepositoryPort
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.album_item import (
    SOURCE_CANDIDATES,
    SOURCE_STAGE,
    SOURCE_TOOL,
    AlbumItem,
)
from kokoro_link.domain.entities.character import Character

_LOGGER = logging.getLogger(__name__)

MAX_ALBUM_ITEMS_PER_CHARACTER = 500
"""Per-character sanity cap. Oldest rows are auto-GC'd on ``add_auto``
when over the cap so a runaway tool loop can't fill the disk. The cap
is much higher than any realistic operator would hit manually; we tune
it down if it becomes a footgun."""

MAX_IMAGES_PER_CHARACTER = 12
"""Duplicated from ``character_image_service`` to avoid a cyclic import.
Kept in sync by the single Character domain entity contract."""


class AlbumServiceError(Exception):
    """Base for album-level errors the route layer maps to HTTP."""


class AlbumItemNotFoundError(AlbumServiceError):
    pass


class AlbumCharacterMismatchError(AlbumServiceError):
    """Raised when the caller tried to act on an item + character pair
    that don't match (e.g. ``?character_id=alice`` but ``item_id`` belongs
    to Bob). Keeps operators from cross-tenant mistakes."""


class StageImageNotFoundError(AlbumServiceError):
    pass


class StageFullError(AlbumServiceError):
    """Raised when ``promote_to_stage`` would overflow the 12-slot cap."""


class AlbumService:
    def __init__(
        self,
        *,
        album_repository: AlbumRepositoryPort,
        character_repository: CharacterRepositoryPort,
        uploads_dir: Path,
        url_prefix: str = "/uploads",
        object_storage: ObjectStoragePort | None = None,
    ) -> None:
        self._album_repository = album_repository
        self._character_repository = character_repository
        _ = uploads_dir, url_prefix
        self._object_storage = object_storage

    async def list_for_character(self, character_id: str) -> list[AlbumItem]:
        return await self._album_repository.list_for_character(character_id)

    async def add_auto(
        self,
        *,
        character_id: str,
        url: str,
        caption: str | None = None,
        byte_size: int | None = None,
    ) -> AlbumItem:
        """Non-raising append used by the tool path.

        When the character is already at the sanity cap we silently
        delete the oldest row (and its file) to make room. The tool
        shouldn't fail mid-chat just because the album is crowded.
        """
        await self._gc_to_fit(character_id, headroom=1)
        item = AlbumItem.create(
            character_id=character_id,
            url=url,
            source=SOURCE_TOOL,
            caption=caption,
            byte_size=byte_size,
        )
        await self._album_repository.add(item)
        _LOGGER.info(
            "album.add_auto character_id=%s url=%s caption=%r size=%s",
            character_id, url, caption, byte_size,
        )
        return item

    async def add_from_candidate(
        self,
        *,
        character_id: str,
        url: str,
        byte_size: int | None = None,
    ) -> AlbumItem:
        """Register a candidate the operator chose to send directly to
        the album (skipping the stage carousel). The file is already at
        its permanent location by the time this is called — the
        candidate-commit path in ``CharacterImageService`` moves the
        bytes out of the temp ``candidates/`` dir first."""
        await self._gc_to_fit(character_id, headroom=1)
        item = AlbumItem.create(
            character_id=character_id,
            url=url,
            source=SOURCE_CANDIDATES,
            byte_size=byte_size,
        )
        await self._album_repository.add(item)
        _LOGGER.info(
            "album.add_from_candidate character_id=%s url=%s size=%s",
            character_id, url, byte_size,
        )
        return item

    async def delete(self, item_id: str) -> AlbumItem:
        item = await self._album_repository.get(item_id)
        if item is None:
            raise AlbumItemNotFoundError(
                f"Album item {item_id!r} not found",
            )
        await self._album_repository.delete(item_id)
        await self._try_delete_media(item.character_id, item.url)
        _LOGGER.info(
            "album.delete character_id=%s item_id=%s url=%s",
            item.character_id, item_id, item.url,
        )
        return item

    async def transfer_from_stage(
        self, *, character_id: str, url: str,
    ) -> tuple[Character, AlbumItem]:
        """Move a stage URL into the album. File is not touched.

        Returns ``(updated_character, new_album_item)``.
        """
        character = await self._load_character(character_id)
        if url not in character.image_urls:
            raise StageImageNotFoundError(
                "Image URL not on this character's stage",
            )
        await self._gc_to_fit(character_id, headroom=1)

        new_urls = tuple(u for u in character.image_urls if u != url)
        updated_character = character.with_image_urls(new_urls)
        await self._character_repository.save(updated_character)

        item = AlbumItem.create(
            character_id=character_id,
            url=url,
            source=SOURCE_STAGE,
        )
        await self._album_repository.add(item)
        _LOGGER.info(
            "album.transfer_from_stage character_id=%s url=%s",
            character_id, url,
        )
        return updated_character, item

    async def promote_to_stage(self, item_id: str) -> Character:
        """Move an album entry into ``Character.image_urls`` (end of list)."""
        item = await self._album_repository.get(item_id)
        if item is None:
            raise AlbumItemNotFoundError(
                f"Album item {item_id!r} not found",
            )
        character = await self._load_character(item.character_id)
        if len(character.image_urls) >= MAX_IMAGES_PER_CHARACTER:
            raise StageFullError(
                f"Stage already has {MAX_IMAGES_PER_CHARACTER} images — "
                "remove one first or keep the item in the album",
            )
        if item.url in character.image_urls:
            # Defensive: if somehow already on stage, just delete the
            # album row and return — still a sensible outcome.
            await self._album_repository.delete(item_id)
            return character

        new_urls = tuple([*character.image_urls, item.url])
        updated = character.with_image_urls(new_urls)
        await self._character_repository.save(updated)
        await self._album_repository.delete(item_id)
        _LOGGER.info(
            "album.promote_to_stage character_id=%s item_id=%s url=%s",
            item.character_id, item_id, item.url,
        )
        return updated

    # ---------- internals ----------

    async def _load_character(self, character_id: str) -> Character:
        character = await self._character_repository.get(character_id)
        if character is None:
            raise AlbumCharacterMismatchError(
                f"Character {character_id!r} does not exist",
            )
        return character

    async def _gc_to_fit(self, character_id: str, *, headroom: int) -> None:
        """Delete oldest entries until there's room for ``headroom`` more.

        No-op when well below the cap. When over or at the cap, delete
        the oldest items + files. Logged so ops can see it happening.
        """
        count = await self._album_repository.count_for_character(character_id)
        if count + headroom <= MAX_ALBUM_ITEMS_PER_CHARACTER:
            return
        # Fetch full list newest-first; we need the tail (oldest).
        items = await self._album_repository.list_for_character(character_id)
        to_evict = count + headroom - MAX_ALBUM_ITEMS_PER_CHARACTER
        victims = items[-to_evict:]
        _LOGGER.warning(
            "album.gc character_id=%s evicting=%d (at cap %d)",
            character_id, len(victims), MAX_ALBUM_ITEMS_PER_CHARACTER,
        )
        for victim in victims:
            await self._album_repository.delete(victim.id)
            await self._try_delete_media(victim.character_id, victim.url)

    async def _try_delete_media(self, character_id: str, url: str) -> None:
        if self._object_storage is None:
            return
        try:
            object_key = self._object_storage.object_key_from_url(url)
            if object_key is not None:
                await self._object_storage.delete(object_key=object_key)
        except Exception:
            _LOGGER.exception(
                "album: failed to delete object for url %s", url,
            )
