"""Export a character + optional arc templates into a ``.lumecard`` blob.

Pipeline (see ``docs/CHARACTER_CARD_PLAN.md`` §4):

1. Load the owned character entity (cross-user access → not found).
2. Project the A-layer settings into the manifest; B / C layers dropped.
3. Resolve the bundled arc templates (the character's bound template
   plus any explicitly requested), serialise each to YAML.
4. Read each stage image's bytes back from object storage.
5. Pack manifest + assets + templates into a single zip.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

from kokoro_link.application.dto.character_card import (
    CHARACTER_CARD_SCHEMA_VERSION,
    CharacterCardArcSeriesBundle,
    CharacterCardManifest,
    CharacterCardMeta,
    CharacterCardProfile,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.contracts.arc_series import ArcSeriesRepositoryPort
from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.infrastructure.character_card.arc_template_yaml import (
    dump_arc_template_to_yaml,
)
from kokoro_link.domain.entities.arc_template import ARC_TEMPLATE_CHARACTER_REF_SELF
from kokoro_link.infrastructure.character_card.packager import (
    STAGE_DIR,
    pack_character_card,
)

_LOGGER = logging.getLogger(__name__)


class CharacterCardError(Exception):
    """Base error for the character-card export/import flow."""


class CharacterCardNotFoundError(CharacterCardError):
    """The requested character doesn't exist or isn't owned by the
    caller (collapsed together to avoid enumeration)."""


@dataclass(frozen=True, slots=True)
class ExportedCard:
    """Result of an export: the zip bytes plus a human-friendly download
    filename (``<slug>.lumecard``)."""

    blob: bytes
    filename: str


class CharacterCardExportService:
    def __init__(
        self,
        *,
        character_service: CharacterService,
        object_storage: ObjectStoragePort,
        arc_template_repository: ArcTemplateRepositoryPort | None = None,
        arc_series_repository: ArcSeriesRepositoryPort | None = None,
        app_version: str = "",
    ) -> None:
        self._character_service = character_service
        self._object_storage = object_storage
        self._arc_template_repository = arc_template_repository
        self._arc_series_repository = arc_series_repository
        self._app_version = app_version

    async def export(
        self,
        character_id: str,
        *,
        user_id: str,
        include_arc_template_ids: list[str] | None = None,
        include_arc_series_ids: list[str] | None = None,
        meta: CharacterCardMeta | None = None,
    ) -> ExportedCard:
        character = await self._character_service.get_character_entity(
            character_id, user_id=user_id,
        )
        if character is None:
            raise CharacterCardNotFoundError(character_id)

        series_bundles, series_ids, series_template_ids = await self._collect_arc_series(
            character_arc_series_id=character.arc_series_id,
            include_arc_series_ids=include_arc_series_ids or [],
            user_id=user_id,
        )
        bundled_files, bundled_ids = await self._collect_arc_templates(
            character_arc_template_id=character.arc_template_id,
            include_arc_template_ids=[
                *(include_arc_template_ids or []),
                *series_template_ids,
            ],
            user_id=user_id,
            character_id=character.id,
        )
        # Only point the imported character at the bound template when it
        # actually made it into the card — otherwise the import would
        # create a character referencing a template the recipient lacks.
        arc_template_ref = (
            character.arc_template_id
            if character.arc_template_id in bundled_ids
            else None
        )
        arc_series_ref = (
            character.arc_series_id
            if character.arc_series_id in series_ids
            else None
        )

        stage_files, stage_paths = await self._collect_stage_images(
            list(character.image_urls),
        )

        profile = CharacterCardProfile.from_domain(
            character,
            arc_template_ref=arc_template_ref,
            arc_series_ref=arc_series_ref,
        )
        card_meta = (meta or CharacterCardMeta()).model_copy()
        if not card_meta.title:
            card_meta.title = character.name
        if not card_meta.created_at:
            card_meta.created_at = datetime.now(timezone.utc).isoformat()
        if not card_meta.app_version:
            card_meta.app_version = self._app_version

        manifest = CharacterCardManifest(
            schema_version=CHARACTER_CARD_SCHEMA_VERSION,
            card=card_meta,
            character=profile,
            stage_images=stage_paths,
            bundled_arc_templates=bundled_ids,
            bundled_arc_series=series_bundles,
        )
        blob = pack_character_card(
            manifest_json=manifest.model_dump_json(indent=2),
            stage_images=stage_files,
            arc_templates=bundled_files,
        )
        return ExportedCard(
            blob=blob, filename=f"{_slugify(character.name)}.lumecard",
        )

    async def _collect_arc_templates(
        self,
        *,
        character_arc_template_id: str | None,
        include_arc_template_ids: list[str],
        user_id: str,
        character_id: str,
    ) -> tuple[list[tuple[str, str]], list[str]]:
        """Resolve + serialise the templates to bundle.

        Returns ``(files, ids)`` where ``files`` are ``(filename,
        yaml)`` for the packager and ``ids`` are the resolved template
        ids (manifest ``bundled_arc_templates``). Order: the bound
        template first, then explicit extras; duplicates dropped."""
        wanted: list[str] = []
        if character_arc_template_id:
            wanted.append(character_arc_template_id)
        for tid in include_arc_template_ids:
            if tid and tid not in wanted:
                wanted.append(tid)

        if not wanted or self._arc_template_repository is None:
            return [], []

        files: list[tuple[str, str]] = []
        ids: list[str] = []
        for tid in wanted:
            template = await self._arc_template_repository.get_for_user(
                tid, user_id=user_id,
            )
            if template is None:
                _LOGGER.warning(
                    "character card export: arc template %s not visible "
                    "to user %s — skipping", tid, user_id,
                )
                continue
            files.append((
                f"{template.id}.yaml",
                dump_arc_template_to_yaml(
                    template,
                    target_character_ref_map={
                        character_id: ARC_TEMPLATE_CHARACTER_REF_SELF,
                    },
                    include_local_target_ids=False,
                ),
            ))
            ids.append(template.id)
        return files, ids

    async def _collect_arc_series(
        self,
        *,
        character_arc_series_id: str | None,
        include_arc_series_ids: list[str],
        user_id: str,
    ) -> tuple[list[CharacterCardArcSeriesBundle], list[str], list[str]]:
        """Resolve series to bundle and collect all member template ids."""
        wanted: list[str] = []
        if character_arc_series_id:
            wanted.append(character_arc_series_id)
        for sid in include_arc_series_ids:
            if sid and sid not in wanted:
                wanted.append(sid)

        if not wanted or self._arc_series_repository is None:
            return [], [], []

        bundles: list[CharacterCardArcSeriesBundle] = []
        ids: list[str] = []
        template_ids: list[str] = []
        for sid in wanted:
            series = await self._arc_series_repository.get_for_user(
                sid, user_id=user_id,
            )
            if series is None:
                _LOGGER.warning(
                    "character card export: arc series %s not visible "
                    "to user %s -- skipping", sid, user_id,
                )
                continue
            bundles.append(CharacterCardArcSeriesBundle.from_domain(series))
            ids.append(series.id)
            for template_id in series.member_template_ids:
                if template_id not in template_ids:
                    template_ids.append(template_id)
        return bundles, ids, template_ids

    async def _collect_stage_images(
        self, image_urls: list[str],
    ) -> tuple[list[tuple[str, bytes]], list[str]]:
        """Read each stage image's bytes back from object storage.

        Returns ``(files, member_paths)``. An image that can't be
        resolved or read is skipped (fail-soft) so one missing object
        doesn't sink the whole export — the manifest just lists fewer
        images."""
        files: list[tuple[str, bytes]] = []
        member_paths: list[str] = []
        for index, url in enumerate(image_urls):
            object_key = self._object_storage.object_key_from_url(url)
            if object_key is None:
                _LOGGER.warning(
                    "character card export: can't derive storage key from "
                    "url %s — skipping", url,
                )
                continue
            try:
                data = await self._object_storage.get_bytes(object_key=object_key)
            except Exception:
                _LOGGER.exception(
                    "character card export: failed to read object %s — "
                    "skipping", object_key,
                )
                continue
            ext = PurePosixPath(object_key).suffix or ".png"
            member_path = f"{STAGE_DIR}{index}{ext}"
            files.append((member_path, data))
            member_paths.append(member_path)
        return files, member_paths


def _slugify(name: str) -> str:
    """Filesystem-friendly slug for the download filename.

    Keeps alphanumerics (incl. CJK) and a few safe separators; collapses
    everything else to ``-``. Falls back to ``character`` when the name
    has no usable characters (e.g. all punctuation)."""
    out: list[str] = []
    for ch in (name or "").strip():
        if ch.isalnum() or ch in ("-", "_"):
            out.append(ch)
        elif ch.isspace():
            out.append("-")
    slug = "".join(out).strip("-")
    return slug or "character"
