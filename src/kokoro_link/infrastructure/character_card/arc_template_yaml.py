"""Serialise an ``ArcTemplate`` to / from the bundled-pack YAML shape.

The export path dumps a template to YAML using the exact field layout
``infrastructure/story/yaml_arc_template_repository`` reads, so an
exported template re-parses into an identical entity on import. The
import path delegates to :func:`build_arc_template_from_mapping` (the
same coercion the bundled loader uses) — no duplicated parsing rules.
"""

from __future__ import annotations

from typing import Any

import yaml

from kokoro_link.domain.entities.arc_template import (
    ARC_TEMPLATE_SCOPE_GENERIC,
    ArcTemplate,
)
from kokoro_link.infrastructure.story.yaml_arc_template_repository import (
    build_arc_template_from_mapping,
)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        cleaned = (value or "").strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return out


def arc_template_to_mapping(
    template: ArcTemplate,
    *,
    target_character_ref_map: dict[str, str] | None = None,
    include_local_target_ids: bool = True,
) -> dict[str, Any]:
    """Project an ``ArcTemplate`` into a plain mapping mirroring the
    bundled-pack YAML schema (``id`` / ``title`` / ``premise`` / ...)."""
    mapping: dict[str, Any] = {
        "id": template.id,
        "title": template.title,
        "premise": template.premise,
        "theme": template.theme,
        "language": template.language,
        "tone": template.tone,
        "duration_days": template.duration_days,
        "binding": {
            "world_frames": list(template.binding.world_frames),
            "required_traits": list(template.binding.required_traits),
        },
        "beats": [
            {
                "sequence": beat.sequence,
                "day_offset": beat.day_offset,
                "title": beat.title,
                "summary": beat.summary,
                "tension": beat.tension,
                "scene_type": beat.scene_type,
                "location": beat.location,
                "scene_characters": list(beat.scene_characters),
                "dramatic_question": beat.dramatic_question,
                "required": beat.required,
            }
            for beat in template.beats
        ],
    }
    applicability = _applicability_mapping(
        template,
        target_character_ref_map=target_character_ref_map or {},
        include_local_target_ids=include_local_target_ids,
    )
    if applicability is not None:
        mapping["applicability"] = applicability
    return mapping


def _applicability_mapping(
    template: ArcTemplate,
    *,
    target_character_ref_map: dict[str, str],
    include_local_target_ids: bool,
) -> dict[str, Any] | None:
    refs = list(template.target_character_refs)
    local_ids: list[str] = []
    for target_id in template.target_character_ids:
        portable_ref = target_character_ref_map.get(target_id)
        if portable_ref:
            refs.append(portable_ref)
        elif include_local_target_ids:
            local_ids.append(target_id)
    refs = _dedupe(refs)
    local_ids = _dedupe(local_ids)
    if (
        template.applicability_scope == ARC_TEMPLATE_SCOPE_GENERIC
        and not refs
        and not local_ids
    ):
        return None
    out: dict[str, Any] = {"scope": template.applicability_scope}
    if local_ids:
        out["target_character_ids"] = local_ids
    if refs:
        out["target_character_refs"] = refs
    return out


def dump_arc_template_to_yaml(
    template: ArcTemplate,
    *,
    target_character_ref_map: dict[str, str] | None = None,
    include_local_target_ids: bool = True,
) -> str:
    """Serialise a template to a UTF-8 YAML string.

    ``allow_unicode`` keeps Chinese titles / premises readable in the
    file; ``sort_keys=False`` preserves the human-authored field order
    so a round-tripped pack file reads naturally in a git diff."""
    return yaml.safe_dump(
        arc_template_to_mapping(
            template,
            target_character_ref_map=target_character_ref_map,
            include_local_target_ids=include_local_target_ids,
        ),
        allow_unicode=True,
        sort_keys=False,
    )


def load_arc_template_from_yaml(text: str, *, fallback_id: str) -> ArcTemplate:
    """Parse a YAML string back into an ``ArcTemplate``.

    ``fallback_id`` is used when the YAML omits ``id`` (mirrors the
    bundled loader's filename-stem fallback). Raises ``ValueError`` on a
    non-mapping top level or a schema the entity rejects."""
    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("arc template YAML top-level must be a mapping")
    return build_arc_template_from_mapping(data, fallback_id=fallback_id)
