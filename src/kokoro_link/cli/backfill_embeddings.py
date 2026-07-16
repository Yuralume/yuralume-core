"""Backfill semantic embeddings for memory items that predate Phase B.

Usage:

    uv run python -m kokoro_link.cli.backfill_embeddings
    uv run python -m kokoro_link.cli.backfill_embeddings --character <id>
    uv run python -m kokoro_link.cli.backfill_embeddings --batch 32 --max 500
    uv run python -m kokoro_link.cli.backfill_embeddings --skip-content
    uv run python -m kokoro_link.cli.backfill_embeddings --skip-tags

Design:

- Two passes per run: first fills any missing **content** embedding
  (legacy rows from before pgvector existed); second fills missing
  **tags_embedding** (rows that had tags but predate the per-tag
  embedding column).
- Uses the configured container so the exact same embedder and DB
  wiring that the app uses at runtime is reused — no divergence risk.
- Each batch is embedded via ``EmbedderPort.embed_many`` (LM Studio
  handles ~32 strings cheaply) and each vector is written back with a
  per-id ``UPDATE``.
- Failures per row are logged and skipped; the loop keeps going so
  one bad item does not halt the whole backfill.
- Exits 0 even when some items fail; the next run picks them up again
  thanks to the ``embedding IS NULL`` / ``tags_embedding IS NULL``
  filters.
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

_DEFAULT_BATCH = 32
_DEFAULT_MAX = 0  # 0 means unlimited


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill vector embeddings for memory items.",
    )
    parser.add_argument(
        "--character",
        default=None,
        help="Limit backfill to a single character id (default: all characters).",
    )
    parser.add_argument(
        "--batch",
        type=int,
        default=_DEFAULT_BATCH,
        help=f"Batch size per embedder call (default: {_DEFAULT_BATCH}).",
    )
    parser.add_argument(
        "--max",
        type=int,
        default=_DEFAULT_MAX,
        help="Stop after N items per pass (0 = unlimited).",
    )
    parser.add_argument(
        "--skip-content",
        action="store_true",
        help="Skip the content-embedding pass (only run tag-embedding pass).",
    )
    parser.add_argument(
        "--skip-tags",
        action="store_true",
        help="Skip the tag-embedding pass (only run content-embedding pass).",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace) -> int:
    settings = AppSettings.from_env()
    if not settings.use_database:
        _LOGGER.error(
            "KOKORO_DATABASE_URL is not set — backfill needs a real repository."
        )
        return 2
    container = build_container(settings)
    await seed_legacy_provider_connections(container, settings)
    await sync_provider_connections(container)
    # Access via the ChatService wiring — the container doesn't expose
    # memory_repo/embedder directly, so we pull them back off the
    # already-assembled ChatService. This keeps the container API
    # surface tight.
    repo = container.chat_service._memory_repository  # noqa: SLF001
    embedder = container.chat_service._embedder  # noqa: SLF001
    if embedder is None or not embedder.is_operational:
        _LOGGER.error(
            "No operational embedding provider configured — set one in "
            "Admin -> Provider Keys before running this backfill."
        )
        return 2

    batch_size = max(1, args.batch)
    limit_max = args.max if args.max > 0 else None

    content_written = 0
    if not args.skip_content:
        content_written = await _backfill_pass(
            label="content",
            fetcher=lambda lim: repo.items_without_embedding(
                limit=lim, character_id=args.character,
            ),
            text_fn=lambda item: item.content,
            persist=repo.update_embedding,
            embedder=embedder,
            batch_size=batch_size,
            limit_max=limit_max,
        )

    tags_written = 0
    if not args.skip_tags:
        tags_written = await _backfill_pass(
            label="tags",
            fetcher=lambda lim: repo.items_pending_tag_embedding(
                limit=lim, character_id=args.character,
            ),
            text_fn=lambda item: " ".join(
                t.strip() for t in item.tags if t and t.strip()
            ),
            persist=repo.update_tags_embedding,
            embedder=embedder,
            batch_size=batch_size,
            limit_max=limit_max,
        )

    print(
        f"Done. Embedded {content_written} content + "
        f"{tags_written} tag vectors."
    )
    return 0


async def _backfill_pass(
    *,
    label: str,
    fetcher,
    text_fn,
    persist,
    embedder,
    batch_size: int,
    limit_max: int | None,
) -> int:
    """Generic batch loop — used by both the content and tag passes.

    Pulls candidates via ``fetcher(limit)``, embeds the strings ``text_fn``
    extracts from each, and writes back via ``persist(item_id, vector)``.
    Returns the number of vectors successfully written.
    """
    processed = 0
    written = 0
    while True:
        remaining = (
            batch_size if limit_max is None
            else min(batch_size, limit_max - processed)
        )
        if remaining <= 0:
            break
        pending = await fetcher(remaining)
        if not pending:
            break
        texts = [text_fn(item) for item in pending]
        vectors = await embedder.embed_many(texts)
        for item, vector in zip(pending, vectors):
            processed += 1
            if vector is None:
                _LOGGER.warning(
                    "[%s] embedding returned None for %s; skipping",
                    label, item.id,
                )
                continue
            try:
                await persist(item.id, vector)
                written += 1
            except Exception:
                _LOGGER.exception(
                    "[%s] failed to persist embedding for %s",
                    label, item.id,
                )
        _LOGGER.info(
            "[%s] backfilled %d / processed %d so far",
            label, written, processed,
        )
    return written


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    args = _parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
