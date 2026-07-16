"""YAML pack loader for the shipped arc templates.

Templates used to be runtime-served straight from this loader. After
migration ``cy0d2e50075`` the authoritative store moved to the
``arc_templates`` DB table; this module's only job is now to read the
YAML files shipped under ``src/kokoro_link/data/arc_templates/`` and
hand the parsed templates to ``ArcTemplatePackSyncService``, which
upserts them as pack rows (``user_id IS NULL``) on every startup.

Keeping the loader narrow:

- No ``ArcTemplateRepositoryPort`` implementation here — the SA repo
  is the runtime port. Tests that need an in-memory port use
  ``InMemoryArcTemplateRepository`` instead.
- No write path. Operator-authored templates are persisted through
  the SA repo, not back to disk.
- Process-lifetime cache stays because the bundled files don't change
  between restarts.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from kokoro_link.domain.entities.arc_template import (
    ArcTemplate,
    ArcTemplateBeat,
    ArcTemplateBinding,
)

_LOGGER = logging.getLogger(__name__)


def default_template_dirs() -> list[Path]:
    """Return the bundled template directory shipped with the repo."""
    here = Path(__file__).resolve()
    # src/kokoro_link/infrastructure/story/yaml_arc_template_repository.py
    # → src/kokoro_link/data/arc_templates
    base = here.parents[2] / "data" / "arc_templates"
    return [base] if base.exists() else []


@dataclass(frozen=True, slots=True)
class LoadedPackTemplate:
    """One YAML file's worth of pack metadata + parsed template.

    ``pack_id`` is the source filename stem — what the sync service
    writes into ``arc_templates.pack_id`` so DB rows can be matched
    back to disk files. ``external_id`` is the original ``id`` field
    declared inside the YAML (when authors override the default of
    ``file stem``), captured for provenance.
    """

    template: ArcTemplate
    pack_id: str
    external_id: str | None


class YAMLArcTemplatePackLoader:
    """Read bundled YAML pack files and return parsed templates.

    Not an ``ArcTemplateRepositoryPort`` — production reads go through
    ``SAArcTemplateRepository``. This class exists solely to feed the
    startup pack sync.
    """

    def __init__(
        self,
        *,
        directories: Iterable[Path] | None = None,
    ) -> None:
        self._directories: tuple[Path, ...] = tuple(
            directories if directories is not None else default_template_dirs()
        )
        self._cache: list[LoadedPackTemplate] | None = None

    def load_all(self) -> list[LoadedPackTemplate]:
        """All packs visible on disk, in stable filename-stem order.

        Repeated calls within the same process serve from cache —
        bundled YAML files don't change at runtime, and the pack sync
        only needs to read them once per process.
        """
        if self._cache is not None:
            return self._cache
        loaded: dict[str, LoadedPackTemplate] = {}
        for directory in self._directories:
            if not directory.exists():
                continue
            for path in sorted(directory.glob("*.yaml")):
                entry = self._load_one(path)
                if entry is None:
                    continue
                if entry.template.id in loaded:
                    _LOGGER.warning(
                        "arc template id collision: %s already loaded; "
                        "skipping %s",
                        entry.template.id, path,
                    )
                    continue
                loaded[entry.template.id] = entry
        self._cache = sorted(loaded.values(), key=lambda e: e.template.id)
        return self._cache

    def reload(self) -> None:
        """Forget the cache so the next ``load_all`` re-parses YAML.

        Useful for tests that mutate template files mid-run; production
        never calls this since the bundled files are immutable.
        """
        self._cache = None

    # ----- internal -------------------------------------------------

    def _load_one(self, path: Path) -> LoadedPackTemplate | None:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            _LOGGER.warning(
                "arc template: failed to read %s — skipping", path,
            )
            return None
        try:
            data = yaml.safe_load(raw)
        except yaml.YAMLError:
            _LOGGER.exception(
                "arc template: YAML parse failed for %s — skipping", path,
            )
            return None
        if not isinstance(data, dict):
            _LOGGER.warning(
                "arc template: %s top-level must be a mapping — skipping",
                path,
            )
            return None
        try:
            template = build_arc_template_from_mapping(data, fallback_id=path.stem)
        except (ValueError, TypeError):
            _LOGGER.exception(
                "arc template: schema rejected for %s — skipping", path,
            )
            return None
        declared_id = _coerce_optional_str(data.get("id"))
        # When the author left ``id`` blank we filled it from the file
        # stem inside ``_build_template``; in that case the declared id
        # is genuinely absent rather than redundant, so we keep
        # ``external_id`` ``None``.
        external_id = (
            declared_id if declared_id and declared_id != path.stem else None
        )
        return LoadedPackTemplate(
            template=template, pack_id=path.stem, external_id=external_id,
        )


def build_arc_template_from_mapping(
    data: dict[str, Any], *, fallback_id: str,
) -> ArcTemplate:
    """Convert one YAML mapping → ``ArcTemplate``.

    Accepts loose typing (string/int/bool coerced sensibly) so authors
    can write the most natural YAML; strict checks live on the entity
    via ``__post_init__``.

    Public so the character-card import path can reuse the exact same
    coercion the bundled-pack loader uses — an exported template's YAML
    must round-trip back to an identical entity.
    """
    raw_beats = data.get("beats")
    if not isinstance(raw_beats, list) or not raw_beats:
        raise ValueError("template missing non-empty 'beats' list")

    beats: list[ArcTemplateBeat] = []
    for index, raw_beat in enumerate(raw_beats):
        if not isinstance(raw_beat, dict):
            raise ValueError(f"beat #{index} must be a mapping")
        beats.append(
            ArcTemplateBeat.create(
                sequence=_coerce_int(
                    raw_beat.get("sequence"), default=index, minimum=0,
                ),
                day_offset=_coerce_int(
                    raw_beat.get("day_offset"), default=0, minimum=0,
                ),
                title=_coerce_str(raw_beat.get("title")),
                summary=_coerce_str(raw_beat.get("summary")),
                tension=_coerce_str(raw_beat.get("tension")) or "setup",
                scene_type=_coerce_str(raw_beat.get("scene_type")) or "encounter",
                location=_coerce_optional_str(raw_beat.get("location")),
                scene_characters=_coerce_str_list(
                    raw_beat.get("scene_characters"),
                ),
                dramatic_question=_coerce_optional_str(
                    raw_beat.get("dramatic_question"),
                ),
                required=_coerce_bool(raw_beat.get("required"), default=True),
            )
        )

    raw_binding = data.get("binding") or {}
    if not isinstance(raw_binding, dict):
        raise ValueError("template 'binding' must be a mapping when present")
    binding = ArcTemplateBinding(
        world_frames=tuple(_coerce_str_list(raw_binding.get("world_frames"))),
        required_traits=tuple(
            _coerce_str_list(raw_binding.get("required_traits")),
        ),
    )
    raw_applicability = data.get("applicability") or {}
    if not isinstance(raw_applicability, dict):
        raise ValueError(
            "template 'applicability' must be a mapping when present",
        )

    template_id = _coerce_str(data.get("id")) or fallback_id
    return ArcTemplate.create(
        id=template_id,
        title=_coerce_str(data.get("title")),
        premise=_coerce_str(data.get("premise")),
        theme=_coerce_str(data.get("theme")) or "custom",
        # language defaults to zh-TW when authors omit it — bundled packs
        # ship Traditional Chinese prose. Metadata only; drives the picker
        # badge + materialise-time translation decision.
        language=_coerce_str(data.get("language")) or "zh-TW",
        duration_days=_coerce_int(
            data.get("duration_days"), default=14, minimum=1,
        ),
        beats=beats,
        binding=binding,
        # tone defaults to "daily" when authors omit it — keeps
        # pre-tone YAMLs reading the same as before.
        tone=_coerce_str(data.get("tone")) or "daily",
        applicability_scope=(
            _coerce_str(raw_applicability.get("scope"))
            or _coerce_str(data.get("applicability_scope"))
            or "generic"
        ),
        target_character_ids=_coerce_str_list(
            raw_applicability.get("target_character_ids")
            if "target_character_ids" in raw_applicability
            else data.get("target_character_ids"),
        ),
        target_character_refs=_coerce_str_list(
            raw_applicability.get("target_character_refs")
            if "target_character_refs" in raw_applicability
            else data.get("target_character_refs"),
        ),
    )


# ----- coercion helpers --------------------------------------------------


def _coerce_str(raw: Any) -> str:
    if isinstance(raw, str):
        return raw.strip()
    if raw is None:
        return ""
    return str(raw).strip()


def _coerce_optional_str(raw: Any) -> str | None:
    s = _coerce_str(raw)
    return s or None


def _coerce_int(raw: Any, *, default: int, minimum: int | None = None) -> int:
    if isinstance(raw, bool):
        # Bool is a subclass of int — guard so True/False don't slip
        # through as 1/0 in numeric fields.
        return default
    if isinstance(raw, int):
        value = raw
    elif isinstance(raw, float):
        value = int(raw)
    elif isinstance(raw, str):
        try:
            value = int(raw.strip())
        except ValueError:
            value = default
    else:
        value = default
    if minimum is not None and value < minimum:
        return minimum
    return value


def _coerce_bool(raw: Any, *, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, (int, float)):
        return bool(raw)
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"true", "yes", "y", "1"}:
            return True
        if lowered in {"false", "no", "n", "0", ""}:
            return False
    return default


def _coerce_str_list(raw: Any) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, str):
        return [chunk.strip() for chunk in raw.split(",") if chunk.strip()]
    if isinstance(raw, list):
        out: list[str] = []
        for entry in raw:
            if not isinstance(entry, str):
                continue
            cleaned = entry.strip()
            if cleaned:
                out.append(cleaned)
        return out
    return []


__all__ = [
    "LoadedPackTemplate",
    "YAMLArcTemplatePackLoader",
    "build_arc_template_from_mapping",
    "default_template_dirs",
]
