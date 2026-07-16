"""Import story-seed YAML packs into the DB.

Idempotent upsert keyed on ``external_id``. Runs reliably at boot, in
CI, and as a CLI invocation — the only requirement is that the YAML
files are well-formed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from kokoro_link.contracts.story import StorySeedRepositoryPort
from kokoro_link.contracts.story_seed_translator import StorySeedTranslatorPort
from kokoro_link.domain.entities.story_seed import StorySeed


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ImportReport:
    packs: int
    seeds_seen: int
    inserted: int
    updated: int
    skipped: int
    errors: tuple[str, ...] = ()


class StorySeedImporter:
    def __init__(
        self,
        repository: StorySeedRepositoryPort,
        *,
        translator: StorySeedTranslatorPort | None = None,
    ) -> None:
        self._repository = repository
        # Optional — when provided together with a target language, each
        # pack's seed_texts are batch-translated before upsert and stamped
        # with the target language for provenance. Fail-soft: a translation
        # miss lands the original text so re-import stays idempotent.
        self._translator = translator

    async def import_paths(
        self,
        paths: Iterable[Path],
        *,
        target_language: str | None = None,
    ) -> ImportReport:
        packs = 0
        seeds_seen = 0
        inserted = 0
        updated = 0
        skipped = 0
        errors: list[str] = []

        for path in paths:
            if not path.is_file():
                errors.append(f"{path}: not a file")
                continue
            try:
                pack = _load_pack(path)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.exception("failed to load pack %s", path)
                errors.append(f"{path}: {exc}")
                continue
            packs += 1
            pack_id = pack.get("pack_id") or path.stem
            pack_language = str(pack.get("language") or "zh-TW").strip() or "zh-TW"
            pack_seeds: list[StorySeed] = []
            for raw_seed in pack.get("seeds", []) or []:
                seeds_seen += 1
                try:
                    pack_seeds.append(
                        _raw_to_domain(
                            raw_seed,
                            default_pack_id=pack_id,
                            default_language=pack_language,
                        ),
                    )
                except Exception as exc:  # noqa: BLE001
                    errors.append(
                        f"{path} seed {raw_seed.get('external_id', '?')}: {exc}",
                    )
                    skipped += 1

            # Batch-translate the whole pack in one call when requested;
            # fail-soft so a translation problem lands the authored text.
            pack_seeds = await self._maybe_translate_pack(
                pack_seeds, target_language=target_language,
            )

            for seed in pack_seeds:
                # We key on external_id, not id — delegating to the repo's
                # dedicated upsert lets SA do the SELECT-by-external_id
                # in-session and keeps this path idempotent.
                try:
                    await self._repository.upsert_by_external_id(seed)
                    # Inserted vs updated is inferred post-hoc by the repo,
                    # but for operator telemetry we only need net-change
                    # counts; treat this as "applied" unconditionally and
                    # let updates share the count with inserts. Keeping
                    # this simple > making repos return more metadata.
                    inserted += 1
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.exception("upsert failed for %s", seed.external_id)
                    errors.append(f"{seed.external_id}: {exc}")
                    skipped += 1

        return self._finish(  # type: ignore[return-value]
            packs=packs, seeds_seen=seeds_seen, inserted=inserted,
            updated=updated, skipped=skipped, errors=errors,
        )

    async def _maybe_translate_pack(
        self,
        seeds: list[StorySeed],
        *,
        target_language: str | None,
    ) -> list[StorySeed]:
        """Batch-localize a pack's seed_texts, fail-soft.

        Returns the seeds untouched when no translator / target language
        is set, the language already matches, or the translator declines
        / errors. Otherwise each seed carries its localized text + the
        target language tag for provenance."""
        target = (target_language or "").strip()
        if not seeds or self._translator is None or not target:
            return seeds
        texts = [s.seed_text for s in seeds]
        try:
            translated = await self._translator.translate_seed_texts(
                texts, target_language=target,
            )
        except Exception:  # noqa: BLE001 — adapters are fail-soft
            _LOGGER.exception("story seed pack translation failed")
            return seeds
        if len(translated) != len(seeds):
            return seeds
        return [
            seed.with_localized_text(new_text, language=target)
            for seed, new_text in zip(seeds, translated)
        ]

    @staticmethod
    def _finish(
        *, packs, seeds_seen, inserted, updated, skipped, errors,
    ) -> ImportReport:
        return ImportReport(
            packs=packs,
            seeds_seen=seeds_seen,
            inserted=inserted,
            updated=updated,
            skipped=skipped,
            errors=tuple(errors),
        )


def _load_pack(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("pack YAML must be a mapping at top level")
    return data


def _raw_to_domain(
    raw: Any, *, default_pack_id: str, default_language: str = "zh-TW",
) -> StorySeed:
    if not isinstance(raw, dict):
        raise ValueError("seed entry must be a mapping")
    external_id = (raw.get("external_id") or "").strip()
    if not external_id:
        raise ValueError("external_id is required for packed seeds")
    seed_text = raw.get("seed_text") or ""
    if not isinstance(seed_text, str) or not seed_text.strip():
        raise ValueError("seed_text must be a non-empty string")

    tags_raw = raw.get("tags") or []
    if not isinstance(tags_raw, list):
        raise ValueError("tags must be a list")
    tags = tuple(str(t) for t in tags_raw if isinstance(t, (str, int)))

    frames_raw = raw.get("world_frames") or ["any"]
    if not isinstance(frames_raw, list):
        raise ValueError("world_frames must be a list")
    frames = tuple(str(f) for f in frames_raw if isinstance(f, str)) or ("any",)

    language = str(raw.get("language") or default_language).strip() or "zh-TW"
    return StorySeed.create(
        seed_text=seed_text,
        tags=list(tags),
        world_frames=list(frames),
        weight=float(raw.get("weight", 1.0)),
        cooldown_days=int(raw.get("cooldown_days", 7)),
        enabled=bool(raw.get("enabled", True)),
        language=language,
        external_id=external_id,
        pack_id=raw.get("pack_id") or default_pack_id,
    )


def default_pack_paths() -> list[Path]:
    """Return bundled pack files. Used by CLI + container bootstrap."""
    root = Path(__file__).resolve().parents[2] / "data" / "story_seeds"
    if not root.is_dir():
        return []
    return sorted(root.glob("*.yaml"))
