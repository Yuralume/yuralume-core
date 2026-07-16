"""Idempotent import of bundled YAML story-seed packs into the DB.

Usage:

    uv run python -m kokoro_link.cli.import_story_seeds
    uv run python -m kokoro_link.cli.import_story_seeds --path custom_pack.yaml

Safe to run on every deploy — the import upserts by ``external_id``.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from kokoro_link.application.services.feature_keys import (
    FEATURE_STORY_SEED_TRANSLATE,
)
from kokoro_link.application.services.story_seed_importer import (
    StorySeedImporter,
    default_pack_paths,
)
from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.infrastructure.story.llm_story_seed_translator import (
    LLMStorySeedTranslator,
)


_LOGGER = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import story-seed YAML packs (idempotent).",
    )
    parser.add_argument(
        "--path",
        action="append",
        default=[],
        help=(
            "Extra pack file to import on top of the bundled packs. "
            "Can be passed multiple times."
        ),
    )
    parser.add_argument(
        "--skip-bundled",
        action="store_true",
        help="Only import paths given via --path; skip the repo's default packs.",
    )
    parser.add_argument(
        "--translate",
        action="store_true",
        help=(
            "LLM-translate each seed's one-line prompt into the operator's "
            "primary language (or --language) before upsert. Fail-soft: a "
            "translation problem lands the original text."
        ),
    )
    parser.add_argument(
        "--language",
        default=None,
        help=(
            "Target language tag for --translate (e.g. en-US). Defaults to "
            "the default operator's primary_language when omitted."
        ),
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = AppSettings.from_env()
    if not settings.use_database:
        _LOGGER.error(
            "KOKORO_DATABASE_URL is not set — import needs a real repository.",
        )
        return 2

    paths: list[Path] = []
    if not args.skip_bundled:
        paths.extend(default_pack_paths())
    for raw in args.path:
        paths.append(Path(raw))

    if not paths:
        _LOGGER.error("No pack files to import.")
        return 2

    container = build_container(settings)

    target_language: str | None = None
    translator = None
    if args.translate:
        target_language = (args.language or "").strip()
        if not target_language:
            target_language = await _default_operator_language(container)
        translator = LLMStorySeedTranslator(
            provider=container.active_llm_provider,
            feature_key=FEATURE_STORY_SEED_TRANSLATE,
        )
        _LOGGER.info("translating seed prompts into %s", target_language)

    importer = StorySeedImporter(
        container.story_seed_repository, translator=translator,
    )
    report = await importer.import_paths(paths, target_language=target_language)

    print(
        f"Done. packs={report.packs} seeds={report.seeds_seen} "
        f"applied={report.inserted} skipped={report.skipped}"
    )
    for err in report.errors:
        print(f"  warning: {err}")
    return 0 if not report.errors else 1


async def _default_operator_language(container) -> str:  # noqa: ANN001
    """Resolve the default operator's primary language, ``zh-TW`` fallback."""
    service = getattr(container, "operator_profile_service", None)
    if service is None:
        return "zh-TW"
    try:
        profile = await service.get_for_user("default")
    except Exception:  # noqa: BLE001 — defensive; CLI must not crash on this
        return "zh-TW"
    return (getattr(profile, "primary_language", None) or "zh-TW").strip() or "zh-TW"


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
