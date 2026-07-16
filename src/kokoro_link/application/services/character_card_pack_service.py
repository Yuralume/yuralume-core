"""Character-card marketplace (MVP) — browse + install bundled packs.

The plan (``docs/CHARACTER_CARD_PLAN.md`` §7) named this
``CharacterCardPackSyncService`` by analogy with the arc-template pack
sync, but the two work differently: arc-template packs are upserted into
a shared ``user_id IS NULL`` table, whereas a character card has no
"shared" form — **installing one creates a fresh character owned by the
installer** via the import path. So this is a catalogue + install
service, not a one-way DB sync. It deliberately reuses
:class:`CharacterCardImportService` so an installed pack and a manually
uploaded ``.lumecard`` go through exactly the same A-layer-only path.
Runtime never travels with the card; only the local install request may
attach an importer-confirmed starting relationship seed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import PurePosixPath

from kokoro_link.application.dto.character_card import (
    CharacterCardManifest,
    CharacterCardPreview,
)
from kokoro_link.application.dto.character import InitialRelationshipPayload
from kokoro_link.application.services.character_card_export_service import (
    CharacterCardError,
)
from kokoro_link.application.services.character_card_import_service import (
    CharacterCardImportService,
    ImportedCard,
)
from kokoro_link.application.services.character_card_preview import (
    build_preview_from_unpacked,
)
from kokoro_link.infrastructure.character_card.pack_catalog import (
    CharacterCardPackCatalog,
)
from kokoro_link.infrastructure.character_card.packager import (
    card_member_image_mime_type,
    unpack_character_card,
)

_LOGGER = logging.getLogger(__name__)


class CharacterCardPackNotFoundError(CharacterCardError):
    """The requested marketplace pack id isn't in the catalogue."""


@dataclass(frozen=True, slots=True)
class CharacterCardPackImage:
    """One stage image extracted from a bundled character-card pack."""

    data: bytes
    content_type: str
    filename: str


class CharacterCardPackSummary(CharacterCardPreview):
    """Display metadata for one marketplace pack, projected from its
    ``manifest.json`` — enough to render a catalogue card without
    downloading or importing anything."""

    pack_id: str


class CharacterCardPackService:
    def __init__(
        self,
        *,
        catalog: CharacterCardPackCatalog,
        import_service: CharacterCardImportService,
    ) -> None:
        self._catalog = catalog
        self._import_service = import_service

    def list_available(self) -> list[CharacterCardPackSummary]:
        """Project every readable bundled pack into a display preview.

        A pack whose blob is unreadable or whose manifest fails schema
        validation is skipped (fail-soft) so one bad file doesn't blank
        the whole marketplace."""
        previews: list[CharacterCardPackSummary] = []
        for pack_id, path in self._catalog.list_pack_files().items():
            try:
                blob = path.read_bytes()
                unpacked = unpack_character_card(blob)
                manifest = CharacterCardManifest.model_validate(unpacked.manifest)
            except Exception:
                _LOGGER.warning(
                    "character card pack %s failed to load — skipping from "
                    "marketplace", pack_id, exc_info=True,
                )
                continue
            preview = build_preview_from_unpacked(
                manifest,
                unpacked,
                pack_id=pack_id,
                image_url_fn=lambda index, _path, _data, pack_id=pack_id: (
                    f"/api/v1/character-cards/{pack_id}/images/{index}"
                ),
            )
            previews.append(
                CharacterCardPackSummary.model_validate(preview.model_dump()),
            )
        return previews

    def get_image(self, pack_id: str, index: int) -> CharacterCardPackImage:
        """Read one bundled stage image by carousel index."""
        blob = self._catalog.read_blob(pack_id)
        if blob is None:
            raise CharacterCardPackNotFoundError(pack_id)
        try:
            unpacked = unpack_character_card(blob)
            manifest = CharacterCardManifest.model_validate(unpacked.manifest)
        except Exception as exc:
            raise CharacterCardPackNotFoundError(pack_id) from exc
        if index < 0 or index >= len(manifest.stage_images):
            raise CharacterCardPackNotFoundError(pack_id)
        member_path = manifest.stage_images[index]
        data = unpacked.stage_images.get(member_path)
        if data is None:
            raise CharacterCardPackNotFoundError(pack_id)
        return CharacterCardPackImage(
            data=data,
            content_type=card_member_image_mime_type(member_path)
            or "application/octet-stream",
            filename=PurePosixPath(member_path).name,
        )

    async def preview(
        self,
        pack_id: str,
        *,
        translate: bool = False,
        target_language: str | None = None,
    ) -> CharacterCardPreview:
        """Preview one bundled pack, optionally translating only this card.

        Listing stays cheap and deterministic; callers use this method
        when the player opts into translation for the current card.
        """
        blob = self._catalog.read_blob(pack_id)
        if blob is None:
            raise CharacterCardPackNotFoundError(pack_id)
        return await self._import_service.preview_card(
            blob,
            translate=translate,
            target_language=target_language,
            pack_id=pack_id,
            image_url_fn=lambda index, _path, _data: (
                f"/api/v1/character-cards/{pack_id}/images/{index}"
            ),
        )

    async def install(
        self,
        pack_id: str,
        *,
        user_id: str,
        translate: bool = False,
        target_language: str | None = None,
        initial_relationship: InitialRelationshipPayload | None = None,
    ) -> ImportedCard:
        """Install a bundled pack as a brand-new character owned by
        ``user_id`` — same import path as a manual ``.lumecard`` upload.

        Raises :class:`CharacterCardPackNotFoundError` when the id isn't
        in the catalogue."""
        blob = self._catalog.read_blob(pack_id)
        if blob is None:
            raise CharacterCardPackNotFoundError(pack_id)
        return await self._import_service.import_card(
            blob,
            user_id=user_id,
            translate=translate,
            target_language=target_language,
            initial_relationship=initial_relationship,
        )


__all__ = [
    "CharacterCardPackImage",
    "CharacterCardPackNotFoundError",
    "CharacterCardPackService",
    "CharacterCardPackSummary",
]
