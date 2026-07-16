"""Filesystem catalogue of bundled ``.lumecard`` packs.

The character-card "marketplace" MVP ships demo characters as
``.lumecard`` files under ``src/kokoro_link/data/character_cards/``
(see ``docs/CHARACTER_CARD_PLAN.md`` §8). Unlike arc-template packs,
these are **not** synced into a shared DB table — installing one runs
the import path to create a brand-new character *owned by the
installer*. So this catalogue is a thin read-only directory index: it
maps a stable ``pack_id`` (the filename stem) to the blob on disk.

A ``CHARACTER_CARD_PACK_DIR`` env override lets a deployment point at an
external pack directory (e.g. a mounted volume) without rebuilding the
image; otherwise the bundled directory is used. Mirrors the
``PROMPTS_DIR`` override convention used by the prompt loader.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

_LOGGER = logging.getLogger(__name__)

_PACK_DIR_ENV = "CHARACTER_CARD_PACK_DIR"
CARD_EXTENSION = ".lumecard"


def default_card_pack_dirs() -> list[Path]:
    """Resolve the pack directory: env override first, then the bundled
    ``data/character_cards/`` shipped with the repo."""
    override = os.environ.get(_PACK_DIR_ENV, "").strip()
    if override:
        path = Path(override).expanduser()
        return [path] if path.exists() else []
    here = Path(__file__).resolve()
    # src/kokoro_link/infrastructure/character_card/pack_catalog.py
    # → src/kokoro_link/data/character_cards
    base = here.parents[2] / "data" / "character_cards"
    return [base] if base.exists() else []


class CharacterCardPackCatalog:
    """Read-only index of the bundled ``.lumecard`` files.

    Stateless beyond the directory list — blobs are read lazily so a
    large pack directory doesn't sit in memory. ``pack_id`` is the
    filename stem; a collision across directories keeps the first
    seen (env override wins over bundled)."""

    def __init__(self, *, directories: list[Path] | None = None) -> None:
        self._directories = (
            directories if directories is not None else default_card_pack_dirs()
        )

    def list_pack_files(self) -> dict[str, Path]:
        """Map ``pack_id`` → file path, in stable id order."""
        found: dict[str, Path] = {}
        for directory in self._directories:
            if not directory.exists():
                continue
            for path in sorted(directory.glob(f"*{CARD_EXTENSION}")):
                pack_id = path.stem
                if pack_id in found:
                    _LOGGER.warning(
                        "character card pack id collision: %s already "
                        "indexed; skipping %s", pack_id, path,
                    )
                    continue
                found[pack_id] = path
        return dict(sorted(found.items()))

    def read_blob(self, pack_id: str) -> bytes | None:
        """Return the raw ``.lumecard`` bytes for ``pack_id``, or ``None``
        when no such pack exists / can't be read."""
        path = self.list_pack_files().get(pack_id)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            _LOGGER.warning(
                "character card pack: failed to read %s — skipping", path,
            )
            return None


__all__ = [
    "CARD_EXTENSION",
    "CharacterCardPackCatalog",
    "default_card_pack_dirs",
]
