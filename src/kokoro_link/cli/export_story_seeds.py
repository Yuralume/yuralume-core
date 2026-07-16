"""Export character-local or per-pack story seeds to YAML.

Round-trip format with ``import_story_seeds`` — exported files can be
shared with the community and re-imported anywhere. Only seeds with a
stable ``external_id`` (i.e. originally imported from a pack) or an
explicit ``--with-local`` flag are exported; per-character UI seeds
default to staying local.

Usage:

    # Export a specific pack to YAML (round-trip the shipped pack)
    uv run python -m kokoro_link.cli.export_story_seeds \\
        --pack core_universal --out /tmp/core_universal.yaml

    # Export all seeds belonging to a character, including their
    # character-local UI seeds (useful for sharing a persona's
    # hand-curated pack).
    uv run python -m kokoro_link.cli.export_story_seeds \\
        --character <id> --with-local --out /tmp/character_pack.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

import yaml

from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.domain.entities.story_seed import StorySeed


_LOGGER = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Export story-seed rows back to YAML.",
    )
    parser.add_argument(
        "--pack", help="Only export seeds with this ``pack_id``.",
    )
    parser.add_argument(
        "--character",
        help="Only export seeds visible to this character (global + private).",
    )
    parser.add_argument(
        "--with-local",
        action="store_true",
        help=(
            "When set, also export seeds that have no ``external_id``. "
            "Those are UI-created per-character seeds; exporting them "
            "creates synthetic ``local:...`` external_ids so a re-import "
            "elsewhere is still idempotent."
        ),
    )
    parser.add_argument(
        "--out", required=True, help="Output YAML path.",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = AppSettings.from_env()
    if not settings.use_database:
        _LOGGER.error("KOKORO_DATABASE_URL not set.")
        return 2

    container = build_container(settings)
    repo = container.story_seed_repository
    if repo is None:
        _LOGGER.error("story seed repository not wired.")
        return 2

    if args.pack:
        seeds = await repo.list_by_pack(args.pack)
    elif args.character:
        seeds = await repo.list_for_character(
            args.character, include_global=True, enabled_only=False,
        )
    else:
        _LOGGER.error("Specify --pack or --character.")
        return 2

    pack_payload = {
        "pack_id": args.pack or f"export_{args.character or 'unknown'}",
        "seeds": [
            _seed_to_yaml(seed, with_local=args.with_local)
            for seed in seeds
            if args.with_local or seed.external_id
        ],
    }
    if not pack_payload["seeds"]:
        _LOGGER.warning(
            "No exportable seeds (run with --with-local to include UI seeds).",
        )
        return 1

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        yaml.safe_dump(
            pack_payload, allow_unicode=True, sort_keys=False,
        ),
        encoding="utf-8",
    )
    print(f"Done. wrote {len(pack_payload['seeds'])} seeds → {out_path}")
    return 0


def _seed_to_yaml(seed: StorySeed, *, with_local: bool) -> dict:
    # Synthesise an external_id for UI seeds being exported; otherwise
    # the importer can't upsert them on the receiving end.
    external_id = seed.external_id or (
        f"local:{seed.id[:8]}" if with_local else None
    )
    payload: dict = {
        "external_id": external_id,
        "seed_text": seed.seed_text,
    }
    if seed.tags:
        payload["tags"] = list(seed.tags)
    if seed.world_frames and seed.world_frames != ("any",):
        payload["world_frames"] = list(seed.world_frames)
    if seed.weight != 1.0:
        payload["weight"] = seed.weight
    if seed.cooldown_days != 7:
        payload["cooldown_days"] = seed.cooldown_days
    if not seed.enabled:
        payload["enabled"] = False
    return payload


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
