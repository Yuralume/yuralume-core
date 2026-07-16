"""Import a ``.lumecard`` blob into a brand-new character.

Pipeline (see ``docs/CHARACTER_CARD_PLAN.md`` §4):

1. Unpack + validate the zip (schema_version, manifest schema).
2. Land each bundled arc template as a row owned by the importer,
   remapping any id that collides with a pack / existing template and
   rewiring the character's ``arc_template_ref`` to the landed id.
3. Create the character from the A-layer profile (B / C layers left at
   their defaults). The caller may attach an importer-confirmed
   ``initial_relationship`` seed for the new local character/operator
   pair; that seed is never read from the card manifest.
4. Re-upload each bundled stage image into the importer's own storage,
   in carousel order, reusing the regular image-add path.

The hard constraint (§6 red line): **no C-layer runtime data crosses
the boundary** — the manifest never carries memories, conversations, or
relationship history. A relationship seed can only come from the
importer during this local confirm request.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import PurePosixPath
from typing import Callable
from uuid import uuid4

from kokoro_link.application.dto.character import (
    CharacterResponse,
    InitialRelationshipPayload,
    UpdateCharacterRequest,
)
from kokoro_link.application.dto.character_card import (
    CHARACTER_CARD_SCHEMA_VERSION,
    CharacterCardArcSeriesBundle,
    CharacterCardManifest,
    CharacterCardPreview,
)
from kokoro_link.application.services.character_card_export_service import (
    CharacterCardError,
)
from kokoro_link.application.services.character_card_preview import (
    build_preview_from_unpacked,
    stage_image_data_url,
)
from kokoro_link.application.services.character_image_service import (
    CharacterImageError,
)
from kokoro_link.application.services.character_service import (
    CharacterService,
    CharacterValidationError,
)
from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.contracts.arc_template_translator import (
    ArcTemplateTranslatorPort,
)
from kokoro_link.contracts.arc_series import ArcSeriesRepositoryPort
from kokoro_link.contracts.character_card_translator import (
    CharacterCardTranslatorPort,
)
from kokoro_link.domain.entities.arc_series import ArcSeries
from kokoro_link.domain.entities.arc_template import (
    ARC_TEMPLATE_CHARACTER_REF_SELF,
    ArcTemplateBinding,
)
from kokoro_link.domain.entities.arc_template import ArcTemplate
from kokoro_link.infrastructure.character_card.arc_template_yaml import (
    load_arc_template_from_yaml,
)
from kokoro_link.infrastructure.character_card.packager import (
    InvalidCharacterCardError,
    UnpackedCard,
    card_member_image_mime_type,
    unpack_character_card,
)

_LOGGER = logging.getLogger(__name__)

# A remapped slug must stay readable but unique — append a short hex
# suffix to the original so the operator can still recognise the
# template in the picker after an id collision.
_REMAP_ATTEMPTS = 5


def _resolve_target_character_refs(
    template: ArcTemplate,
    *,
    target_character_ref_map: dict[str, str],
) -> ArcTemplate:
    if not template.target_character_refs:
        return template
    ids = list(template.target_character_ids)
    unresolved: list[str] = []
    for ref in template.target_character_refs:
        local_id = target_character_ref_map.get(ref)
        if local_id:
            ids.append(local_id)
        else:
            unresolved.append(ref)
    return template.with_target_character_ids(
        ids,
        target_character_refs=unresolved,
    )


class CharacterCardImportError(CharacterCardError):
    """The card couldn't be imported (bad blob, unreadable manifest, or
    an unsupported schema version)."""


class UnsupportedCardSchemaError(CharacterCardImportError):
    """The card declares a ``schema_version`` newer than this build can
    read — refuse rather than silently dropping fields we don't know."""


@dataclass(frozen=True, slots=True)
class ImportedCard:
    """Result of an import: the freshly created character plus the ids of
    the arc templates that were landed (after any collision remap), so
    the caller / UI can report what was installed alongside the
    character."""

    character: CharacterResponse
    landed_arc_template_ids: list[str]
    landed_arc_series_ids: list[str]


class CharacterCardImportService:
    def __init__(
        self,
        *,
        character_service: CharacterService,
        character_image_service,
        arc_template_repository: ArcTemplateRepositoryPort | None = None,
        arc_series_repository: ArcSeriesRepositoryPort | None = None,
        translator: CharacterCardTranslatorPort | None = None,
        arc_template_translator: ArcTemplateTranslatorPort | None = None,
    ) -> None:
        self._character_service = character_service
        self._character_image_service = character_image_service
        self._arc_template_repository = arc_template_repository
        self._arc_series_repository = arc_series_repository
        self._translator = translator
        # Optional — when a bundled ``.lumecard`` carries arc templates and
        # the importer asked to translate ("翻成我的語言"), each template's
        # player-visible prose is localized before it lands as an importer-
        # owned row, closing the gap where translate only touched the
        # profile. Per-template fail-soft: one template's failure lands the
        # original prose rather than skipping the whole card.
        self._arc_template_translator = arc_template_translator

    async def import_card(
        self,
        blob: bytes,
        *,
        user_id: str,
        translate: bool = False,
        target_language: str | None = None,
        initial_relationship: InitialRelationshipPayload | None = None,
    ) -> ImportedCard:
        unpacked = unpack_character_card(blob)
        manifest = self._validate_manifest(unpacked)
        manifest = await self._maybe_translate_manifest(
            manifest,
            translate=translate,
            target_language=target_language,
        )

        create_request = manifest.character.to_create_request(
            image_urls=[],
            arc_template_id=None,
            arc_series_id=None,
        )
        if initial_relationship is not None:
            create_request = create_request.model_copy(
                update={"initial_relationship": initial_relationship},
            )
        created = await self._character_service.create_character(
            create_request, user_id=user_id,
        )

        arc_id_map, landed_ids = await self._land_arc_templates(
            unpacked.arc_templates,
            user_id=user_id,
            target_character_ref_map={
                ARC_TEMPLATE_CHARACTER_REF_SELF: created.id,
            },
            translate=translate,
            target_language=target_language,
        )
        resolved_arc_id = self._resolve_arc_template_id(
            manifest.character.arc_template_ref, arc_id_map, landed_ids,
        )
        series_id_map, landed_series_ids = await self._land_arc_series(
            manifest.bundled_arc_series,
            arc_id_map=arc_id_map,
            landed_template_ids=landed_ids,
            user_id=user_id,
        )
        resolved_series_id = self._resolve_arc_series_id(
            manifest.character.arc_series_ref,
            series_id_map,
            landed_series_ids,
        )
        update_payload: dict[str, str] = {}
        if resolved_arc_id is not None:
            update_payload["arc_template_id"] = resolved_arc_id
        if resolved_series_id is not None:
            update_payload["arc_series_id"] = resolved_series_id
        if update_payload:
            try:
                updated = await self._character_service.update_character(
                    created.id,
                    UpdateCharacterRequest(**update_payload),
                    user_id=user_id,
                )
            except CharacterValidationError as exc:
                raise CharacterCardImportError(str(exc)) from exc
            if updated is not None:
                created = updated

        await self._reupload_stage_images(
            created.id, manifest.stage_images, unpacked.stage_images,
        )

        # Re-read so the response reflects the appended stage images.
        final = await self._character_service.get_character(
            created.id, user_id=user_id,
        )
        return ImportedCard(
            character=final or created,
            landed_arc_template_ids=landed_ids,
            landed_arc_series_ids=landed_series_ids,
        )

    async def preview_card(
        self,
        blob: bytes,
        *,
        translate: bool = False,
        target_language: str | None = None,
        pack_id: str | None = None,
        image_url_fn: Callable[[int, str, bytes], str | None] | None = None,
    ) -> CharacterCardPreview:
        """Validate and project a card without creating any rows.

        This intentionally reuses the same unpack + schema-version
        validation as ``import_card`` so a preview cannot succeed for a
        card that the actual import path would reject.
        """
        unpacked = unpack_character_card(blob)
        manifest = self._validate_manifest(unpacked)
        manifest = await self._maybe_translate_manifest(
            manifest,
            translate=translate,
            target_language=target_language,
        )
        resolved_image_url_fn = image_url_fn or (
            lambda _index, path, data: stage_image_data_url(path, data)
        )
        return build_preview_from_unpacked(
            manifest,
            unpacked,
            pack_id=pack_id,
            image_url_fn=resolved_image_url_fn,
            prefer_profile_text=translate,
        )

    def _validate_manifest(
        self, unpacked: UnpackedCard,
    ) -> CharacterCardManifest:
        declared = unpacked.manifest.get("schema_version")
        if isinstance(declared, int) and declared > CHARACTER_CARD_SCHEMA_VERSION:
            raise UnsupportedCardSchemaError(
                f"card schema_version {declared} is newer than this build "
                f"supports ({CHARACTER_CARD_SCHEMA_VERSION})",
            )
        try:
            return CharacterCardManifest.model_validate(unpacked.manifest)
        except (ValueError, TypeError) as exc:
            # pydantic ValidationError is a ValueError subclass.
            raise CharacterCardImportError(
                "manifest.json does not match the character-card schema",
            ) from exc

    async def _maybe_translate_manifest(
        self,
        manifest: CharacterCardManifest,
        *,
        translate: bool,
        target_language: str | None,
    ) -> CharacterCardManifest:
        target = (target_language or "").strip()
        if not translate or not target or self._translator is None:
            return manifest
        try:
            translated = await self._translator.translate_profile(
                manifest.character,
                target_language=target,
            )
        except Exception:  # pragma: no cover — adapters are expected fail-soft
            _LOGGER.exception("character card import: translator failed")
            return manifest
        if translated == manifest.character:
            return manifest
        return manifest.model_copy(update={"character": translated})

    async def _maybe_translate_template(
        self,
        template: ArcTemplate,
        *,
        translate: bool,
        target_language: str | None,
    ) -> ArcTemplate:
        """Localize one bundled template's prose, fail-soft per template.

        Returns the original template unchanged when translation was not
        requested, no target language / translator is available, or the
        translator raised / declined."""
        target = (target_language or "").strip()
        if (
            not translate
            or not target
            or self._arc_template_translator is None
        ):
            return template
        try:
            return await self._arc_template_translator.translate_template(
                template, target_language=target,
            )
        except Exception:  # pragma: no cover — adapters are fail-soft
            _LOGGER.exception(
                "character card import: arc template translator failed "
                "for %s", template.id,
            )
            return template

    async def _land_arc_templates(
        self,
        arc_templates: dict[str, str],
        *,
        user_id: str,
        target_character_ref_map: dict[str, str],
        translate: bool = False,
        target_language: str | None = None,
    ) -> tuple[dict[str, str], list[str]]:
        """Parse + persist each bundled template as a row owned by the
        importer.

        When ``translate`` is set with a ``target_language`` and an arc
        template translator is wired, each template's player-visible prose
        is localized before it lands — closing the gap where translate
        only touched ``manifest.character``. Per-template fail-soft: a
        translation failure lands the original prose rather than skipping.

        Returns ``(id_map, landed_ids)`` where ``id_map`` maps an
        original bundled id to its remapped id (only when a collision
        forced a rename) and ``landed_ids`` are the ids actually written
        (post-remap), in filename order."""
        if not arc_templates or self._arc_template_repository is None:
            return {}, []

        id_map: dict[str, str] = {}
        landed_ids: list[str] = []
        for filename in sorted(arc_templates):
            yaml_text = arc_templates[filename]
            fallback_id = PurePosixPath(filename).stem
            try:
                template = load_arc_template_from_yaml(
                    yaml_text, fallback_id=fallback_id,
                )
            except (ValueError, TypeError):
                _LOGGER.warning(
                    "character card import: arc template %s failed to "
                    "parse — skipping", filename,
                )
                continue
            template = _resolve_target_character_refs(
                template,
                target_character_ref_map=target_character_ref_map,
            )
            template = await self._maybe_translate_template(
                template,
                translate=translate,
                target_language=target_language,
            )
            landed = await self._save_with_remap(template, user_id=user_id)
            if landed is None:
                continue
            if landed.id != template.id:
                id_map[template.id] = landed.id
            landed_ids.append(landed.id)
        return id_map, landed_ids

    async def _save_with_remap(
        self, template: ArcTemplate, *, user_id: str,
    ) -> ArcTemplate | None:
        """Persist ``template``; on an id collision (pack / existing /
        other user's row) retry with a fresh suffixed id.

        ``save_for_user`` is the single source of truth on collisions —
        it raises ``ValueError`` for every collision class, so we don't
        have to pre-check each one. Returns the actually-saved template
        (its id may differ from the input), or ``None`` if it couldn't be
        landed after a few attempts."""
        assert self._arc_template_repository is not None
        candidate = template
        for _ in range(_REMAP_ATTEMPTS):
            try:
                await self._arc_template_repository.save_for_user(
                    candidate, user_id=user_id, overwrite=False,
                )
                return candidate
            except ValueError:
                candidate = replace(
                    template, id=f"{template.id}-{uuid4().hex[:8]}",
                )
        _LOGGER.warning(
            "character card import: could not land arc template %s after "
            "%d remap attempts — skipping", template.id, _REMAP_ATTEMPTS,
        )
        return None

    async def _land_arc_series(
        self,
        bundled_series: list[CharacterCardArcSeriesBundle],
        *,
        arc_id_map: dict[str, str],
        landed_template_ids: list[str],
        user_id: str,
    ) -> tuple[dict[str, str], list[str]]:
        """Persist bundled series as importer-owned rows.

        Member refs are rewired to the landed template ids. A series is
        skipped if any member template failed to land, preventing dangling
        authoring refs from entering the importer's workspace.
        """
        if not bundled_series or self._arc_series_repository is None:
            return {}, []

        id_map: dict[str, str] = {}
        landed_ids: list[str] = []
        for bundled in bundled_series:
            member_ids: list[str] = []
            missing_ref = False
            for member in sorted(bundled.members, key=lambda item: item.position):
                remapped = arc_id_map.get(member.template_ref, member.template_ref)
                if remapped not in landed_template_ids:
                    missing_ref = True
                    break
                if remapped not in member_ids:
                    member_ids.append(remapped)
            if missing_ref or not member_ids:
                _LOGGER.warning(
                    "character card import: arc series %s references "
                    "templates that were not landed -- skipping", bundled.id,
                )
                continue
            try:
                series = ArcSeries.create(
                    id=bundled.id,
                    title=bundled.title,
                    premise=bundled.premise,
                    theme=bundled.theme,
                    tone=bundled.tone,
                    binding=ArcTemplateBinding(
                        world_frames=tuple(bundled.binding.world_frames),
                        required_traits=tuple(bundled.binding.required_traits),
                    ),
                    template_ids=member_ids,
                    user_id=user_id,
                )
            except ValueError:
                _LOGGER.warning(
                    "character card import: arc series %s failed to parse -- "
                    "skipping", bundled.id,
                )
                continue
            landed = await self._save_series_with_remap(series, user_id=user_id)
            if landed is None:
                continue
            if landed.id != series.id:
                id_map[series.id] = landed.id
            landed_ids.append(landed.id)
        return id_map, landed_ids

    async def _save_series_with_remap(
        self, series: ArcSeries, *, user_id: str,
    ) -> ArcSeries | None:
        """Persist ``series``; retry with readable suffixed ids on collision."""
        assert self._arc_series_repository is not None
        candidate = series
        for _ in range(_REMAP_ATTEMPTS):
            try:
                await self._arc_series_repository.save_for_user(
                    candidate, user_id=user_id, overwrite=False,
                )
                return candidate
            except ValueError:
                candidate = ArcSeries.create(
                    id=f"{series.id}-{uuid4().hex[:8]}",
                    title=series.title,
                    premise=series.premise,
                    theme=series.theme,
                    tone=series.tone,
                    binding=series.binding,
                    template_ids=series.member_template_ids,
                    user_id=user_id,
                )
        _LOGGER.warning(
            "character card import: could not land arc series %s after "
            "%d remap attempts -- skipping", series.id, _REMAP_ATTEMPTS,
        )
        return None

    def _resolve_arc_series_id(
        self,
        ref: str | None,
        id_map: dict[str, str],
        landed_ids: list[str],
    ) -> str | None:
        """Rewire ``arc_series_ref`` to the local imported series id."""
        if not ref:
            return None
        remapped = id_map.get(ref, ref)
        if remapped in landed_ids:
            return remapped
        return None

    def _resolve_arc_template_id(
        self,
        ref: str | None,
        id_map: dict[str, str],
        landed_ids: list[str],
    ) -> str | None:
        """Rewire the manifest's ``arc_template_ref`` to the landed id.

        Returns the local id the imported character should bind to, or
        ``None`` when the referenced template wasn't landed (so the
        character imports without a dangling arc binding)."""
        if not ref:
            return None
        remapped = id_map.get(ref, ref)
        if remapped in landed_ids:
            return remapped
        return None

    async def _reupload_stage_images(
        self,
        character_id: str,
        member_paths: list[str],
        stage_bytes: dict[str, bytes],
    ) -> None:
        """Append each bundled stage image to the new character, in the
        manifest's carousel order.

        Reuses the regular image-add path so the objects land under the
        importer's own ``characters/{id}/...`` keys with fresh urls. A
        missing / oversized / unsupported image is skipped (fail-soft) so
        one bad asset doesn't sink the whole import."""
        for path in member_paths:
            data = stage_bytes.get(path)
            if data is None:
                _LOGGER.warning(
                    "character card import: stage image %s listed in "
                    "manifest but absent from the zip — skipping", path,
                )
                continue
            mime = card_member_image_mime_type(path)
            try:
                await self._character_image_service.add_image(
                    character_id,
                    data=data,
                    mime_type=mime,
                    original_filename=PurePosixPath(path).name,
                )
            except CharacterImageError:
                _LOGGER.warning(
                    "character card import: stage image %s rejected by the "
                    "image service — skipping", path, exc_info=True,
                )


__all__ = [
    "CharacterCardImportError",
    "CharacterCardImportService",
    "ImportedCard",
    "InvalidCharacterCardError",
    "UnsupportedCardSchemaError",
]
