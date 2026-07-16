"""Run memory consolidation + decay from the command line.

Usage:

    uv run python -m kokoro_link.cli.consolidate_memories --character <id>
    uv run python -m kokoro_link.cli.consolidate_memories --character <id> --dry-run
    uv run python -m kokoro_link.cli.consolidate_memories --character <id> --decay-only
    uv run python -m kokoro_link.cli.consolidate_memories --all --dry-run

Omit ``--character`` with ``--all`` to iterate every character that has
at least one memory — useful for a nightly cleanup run.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from kokoro_link.bootstrap.container import build_container
from kokoro_link.bootstrap.settings import AppSettings
from kokoro_link.infrastructure.provider_settings.runtime_sync import (
    seed_legacy_provider_connections,
    sync_provider_connections,
)

_LOGGER = logging.getLogger(__name__)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Consolidate + decay memories for one or all characters.",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--character", help="Character id to process.")
    target.add_argument(
        "--all", action="store_true", help="Iterate every character.",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--decay-only", action="store_true")
    parser.add_argument(
        "--similarity", type=float, default=None,
        help="Cosine similarity threshold for clustering (default: 0.82).",
    )
    parser.add_argument(
        "--min-cluster", type=int, default=None,
        help="Minimum cluster size to trigger a merge (default: 2).",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = AppSettings.from_env()
    if not settings.use_database:
        _LOGGER.error("KOKORO_DATABASE_URL must be set.")
        return 2

    container = build_container(settings)
    await seed_legacy_provider_connections(container, settings)
    await sync_provider_connections(container)
    service = container.memory_consolidation_service

    kwargs: dict = {
        "dry_run": args.dry_run,
        "decay_only": args.decay_only,
    }
    if args.similarity is not None:
        kwargs["similarity_threshold"] = args.similarity
    if args.min_cluster is not None:
        kwargs["min_cluster_size"] = args.min_cluster

    if args.character:
        report = await service.consolidate(args.character, **kwargs)
        _print(report)
        return 0

    # --all mode: pull every character via the character service.
    characters = await container.character_service.list_characters()
    for char in characters:
        report = await service.consolidate(char.id, **kwargs)
        _print(report, label=char.name)
    return 0


def _print(report, *, label: str | None = None) -> None:  # noqa: ANN001
    header = (
        f"[{label or report.character_id}] "
        f"{'DRY-RUN — ' if report.dry_run else ''}"
        f"decayed={report.decayed} "
        f"clusters={report.clusters_found} "
        f"merged={report.clusters_merged} "
        f"replaced={report.memories_replaced} "
        f"after={report.memories_after}"
    )
    print(header)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
