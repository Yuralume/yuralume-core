"""Character-card catalogue — browse + install, from Cloud and from disk.

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

Since ``OFFICIAL_CARD_CLOUD_CATALOG_PLAN`` (D1/D5) the catalogue has two
sources and this service is where they meet:

* **Cloud** — the official cards, read anonymously from the public catalog
  by :class:`OfficialCardPackSource`. Fail-soft: unreachable means an empty
  official shelf and a flag, never an error.
* **Local** — the ``.lumecard`` files in the
  :class:`CharacterCardPackCatalog` directory. The repo no longer ships any,
  so this is empty by default; it stays wired because pointing
  ``CHARACTER_CARD_PACK_DIR`` at an own folder is how an air-gapped
  deployment keeps a card shelf with no outbound connection at all.

One id space, one opaque ``pack_id``: a local pack keeps its filename stem
and a Cloud card is prefixed ``cloud:``. The frontend hands whichever it was
given straight back, and the prefix is what routes preview / install to the
right source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
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
from kokoro_link.application.services.official_card_pack_source import (
    OfficialCardPackSource,
    OfficialCardUnavailableError,
)
from kokoro_link.contracts.official_card_catalog import (
    OfficialCardNotFound,
    parse_cloud_pack_ref,
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


@dataclass(frozen=True, slots=True)
class CharacterCardCatalogue:
    """Everything the browse grid needs, including what is missing.

    ``official_cards_unavailable`` is a flag rather than an error because
    this is a mirror, not a wall: a Cloud outage removes the official shelf
    and says so, while local packs and every installed character carry on.
    It stays ``False`` when the catalog is switched off entirely — nothing is
    wrong then, there is simply nothing there.
    """

    cards: list[CharacterCardPackSummary] = field(default_factory=list)
    official_cards_unavailable: bool = False


class CharacterCardPackService:
    def __init__(
        self,
        *,
        catalog: CharacterCardPackCatalog,
        import_service: CharacterCardImportService,
        official_cards: OfficialCardPackSource | None = None,
    ) -> None:
        self._catalog = catalog
        self._import_service = import_service
        self._official_cards = official_cards

    async def list_available(
        self, *, primary_language: str = "",
    ) -> CharacterCardCatalogue:
        """Project the Cloud catalog and every readable local pack.

        Official cards come first — they are what the shelf is for — and a
        local pack whose blob is unreadable or whose manifest fails schema
        validation is skipped (fail-soft) so one bad file doesn't blank the
        whole gallery.
        """
        cards: list[CharacterCardPackSummary] = []
        unavailable = False
        if self._official_cards is not None:
            official = await self._official_cards.list_summaries(
                primary_language=primary_language,
            )
            if official is None:
                unavailable = True
            else:
                cards.extend(_as_summary(preview) for preview in official)
        cards.extend(self._local_summaries())
        return CharacterCardCatalogue(
            cards=cards, official_cards_unavailable=unavailable,
        )

    def get_image(self, pack_id: str, index: int) -> CharacterCardPackImage:
        """Read one **local** bundled stage image by carousel index.

        Official cards never come through here: their images are absolute
        Cloud URLs the browser fetches directly, which is what keeps a
        gallery of official thumbnails off this deployment entirely.
        """
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
        primary_language: str = "",
    ) -> CharacterCardPreview:
        """Preview one pack, optionally translating only this card.

        Listing stays cheap and deterministic; callers use this method
        when the player opts into translation for the current card.

        An official card takes neither branch: the catalog already holds an
        approved translation per language, so its preview is one cached read
        and no model call regardless of the ``translate`` flag.
        """
        card_id = parse_cloud_pack_ref(pack_id)
        if card_id is not None:
            return await self._official_preview(
                card_id, primary_language=primary_language,
            )
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
        primary_language: str = "",
        initial_relationship: InitialRelationshipPayload | None = None,
    ) -> ImportedCard:
        """Install a pack as a brand-new character owned by ``user_id`` —
        same import path as a manual ``.lumecard`` upload, whether the card
        came off disk or down from the Cloud catalog. What lands is fully
        local either way; an installed character never reads the catalog
        again.

        ``translate`` applies to **local** packs only. An official card is
        published with an approved translation per language and never reaches
        a model, which is why the gallery hides its translate toggle — and
        why the flag must not sneak in through this door either.

        Raises :class:`CharacterCardPackNotFoundError` when the id isn't
        in the catalogue."""
        card_id = parse_cloud_pack_ref(pack_id)
        if card_id is not None:
            # ``translate`` / ``target_language`` deliberately stop here: an
            # official card is published with an approved translation per
            # language and never reaches a model, so forwarding the flag
            # would bill a player for a toggle the gallery does not even show
            # them on this card (plan §3.5 + the cloud-card ruling).
            return await self._official_install(
                card_id,
                user_id=user_id,
                primary_language=primary_language,
                initial_relationship=initial_relationship,
            )
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

    def _local_summaries(self) -> list[CharacterCardPackSummary]:
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
            previews.append(_as_summary(preview))
        return previews

    async def _official_preview(
        self, card_id: str, *, primary_language: str,
    ) -> CharacterCardPreview:
        if self._official_cards is None:
            raise CharacterCardPackNotFoundError(card_id)
        try:
            return await self._official_cards.preview(
                card_id, primary_language=primary_language,
            )
        except OfficialCardNotFound as exc:
            raise CharacterCardPackNotFoundError(card_id) from exc

    async def _official_install(
        self,
        card_id: str,
        *,
        user_id: str,
        primary_language: str,
        initial_relationship: InitialRelationshipPayload | None,
    ) -> ImportedCard:
        if self._official_cards is None:
            raise CharacterCardPackNotFoundError(card_id)
        try:
            return await self._official_cards.install(
                card_id,
                user_id=user_id,
                primary_language=primary_language,
                initial_relationship=initial_relationship,
            )
        except OfficialCardNotFound as exc:
            raise CharacterCardPackNotFoundError(card_id) from exc


def _as_summary(preview: CharacterCardPreview) -> CharacterCardPackSummary:
    return CharacterCardPackSummary.model_validate(preview.model_dump())


__all__ = [
    "CharacterCardCatalogue",
    "CharacterCardPackImage",
    "CharacterCardPackNotFoundError",
    "CharacterCardPackService",
    "CharacterCardPackSummary",
    "OfficialCardUnavailableError",
]
