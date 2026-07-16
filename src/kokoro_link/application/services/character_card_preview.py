"""Shared preview projection for character-card browse/import flows."""

from __future__ import annotations

from base64 import b64encode
from pathlib import PurePosixPath
from typing import Callable

from kokoro_link.application.dto.character_card import (
    CharacterCardManifest,
    CharacterCardPreview,
    build_card_preview,
)
from kokoro_link.infrastructure.character_card.arc_template_yaml import (
    load_arc_template_from_yaml,
)
from kokoro_link.infrastructure.character_card.packager import (
    UnpackedCard,
    card_member_image_mime_type,
)


def build_preview_from_unpacked(
    manifest: CharacterCardManifest,
    unpacked: UnpackedCard,
    *,
    image_url_fn: Callable[[int, str, bytes], str | None],
    pack_id: str | None = None,
    prefer_profile_text: bool = False,
) -> CharacterCardPreview:
    """Build a preview, skipping manifest-listed images absent from zip."""

    def resolve_image_url(index: int, member_path: str) -> str | None:
        data = unpacked.stage_images.get(member_path)
        if data is None:
            return None
        return image_url_fn(index, member_path, data)

    return build_card_preview(
        manifest,
        pack_id=pack_id,
        image_url_fn=resolve_image_url,
        bundled_arc_titles=_bundled_arc_titles(manifest, unpacked),
        prefer_profile_text=prefer_profile_text,
    )


def stage_image_data_url(member_path: str, data: bytes) -> str:
    """Encode an unpacked stage image for one-off upload preview."""
    mime = card_member_image_mime_type(member_path) or "application/octet-stream"
    encoded = b64encode(data).decode("ascii")
    return f"data:{mime};base64,{encoded}"


def _bundled_arc_titles(
    manifest: CharacterCardManifest,
    unpacked: UnpackedCard,
) -> list[str]:
    title_by_id: dict[str, str] = {}
    fallback_by_id: dict[str, str] = {}
    for filename, yaml_text in unpacked.arc_templates.items():
        fallback_id = PurePosixPath(filename).stem
        fallback_by_id[fallback_id] = fallback_id
        try:
            template = load_arc_template_from_yaml(
                yaml_text, fallback_id=fallback_id,
            )
        except (ValueError, TypeError):
            continue
        title_by_id[template.id] = template.title or template.id
        fallback_by_id[template.id] = template.title or template.id

    return [
        title_by_id.get(template_id)
        or fallback_by_id.get(template_id)
        or template_id
        for template_id in manifest.bundled_arc_templates
    ]
