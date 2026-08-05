"""Backfill WebP variants for images that predate the variant pipeline (IV3).

``VariantAwareObjectStorage`` (IV1) only sees images written *after* it was
deployed. Everything already in the bucket serves as the original PNG
forever, because ``GET /v1/public/{key}?v=w320`` falls back to the original
whenever the variant is absent (IV2) — correct, silent, and exactly the
2.3 MB-per-image cost the plan set out to remove. This script closes that
gap for the existing stock.

Run it **on the deployment host** (``play.yuralume.com`` — an OCI VPS, not
a developer laptop): it needs the production ``DATABASE_URL`` and
``STORAGE_URL``/``STORAGE_KEY`` from the app's own environment, and moves
several GB between the database, this process and the storage service.

    # 1. sample twenty images, write nothing, read the numbers
    uv run python scripts/backfill_image_variants.py --dry-run --limit 20

    # 2. the real thing (safe to interrupt and re-run — see below)
    uv run python scripts/backfill_image_variants.py

Idempotent and resumable with no bookkeeping: an image whose variants are
already in storage costs three metadata probes and is skipped, so a run
that is interrupted at 80% simply re-probes the first 80% and continues.
Ctrl-C prints the totals achieved so far and exits 130.

Exit codes: ``0`` clean, ``1`` finished with per-image failures, ``2``
configuration/usage error, ``130`` interrupted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.exc import SQLAlchemyError

_CORE_ROOT = Path(__file__).resolve().parents[1]
for _path in (_CORE_ROOT, _CORE_ROOT / "src"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from scripts.image_variant_backfill.runner import (  # noqa: E402
    DEFAULT_CONCURRENCY,
    DEFAULT_PROGRESS_EVERY,
    MAX_CONCURRENCY,
    BackfillAccumulator,
    BackfillProgress,
    format_stats,
    make_thread_encoder,
    run_backfill,
)
from scripts.image_variant_backfill.sources import (  # noqa: E402
    ObjectKeyInventory,
    build_inventory,
    iter_url_references,
)

_LOGGER = logging.getLogger("backfill_image_variants")

_MAX_REPORTED_ERRORS = 20
"""Per-image failures echoed at the end. The rest are already in the log —
a batch that fails on thousands of images should not bury its own summary."""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate the three WebP renditions (w320/w768/full) for every "
            "image still referenced by character_album_items.url, "
            "characters.image_urls or feed_posts.image_url. Idempotent; "
            "safe to interrupt and re-run."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Do everything except write or delete. Originals are still "
            "downloaded and encoded, so the reported byte savings are "
            "measured rather than estimated; pair it with --limit for a "
            "cheap sample."
        ),
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help=(
            "Process at most N distinct object keys. Caps unique images, not "
            "DB rows (the same picture is commonly referenced by both the "
            "album and the stage, and a manual feed post can point at "
            "either). Resuming a full run does not need this."
        ),
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        metavar="N",
        help=(
            f"Images in flight at once (default {DEFAULT_CONCURRENCY}, max "
            f"{MAX_CONCURRENCY}). Budget roughly 40 MB of RSS and one "
            "encoder thread per slot; the default is sized to leave a small "
            "VPS able to keep serving traffic."
        ),
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=DEFAULT_PROGRESS_EVERY,
        metavar="N",
        help="Emit a progress line every N images (default %(default)s).",
    )
    parser.add_argument(
        "--prune-originals",
        action="store_true",
        help=(
            "DANGEROUS, IRREVERSIBLE, OFF BY DEFAULT (plan D10). Deletes each "
            "original once all of its variants exist. Note that every stored "
            "URL is '/v1/public/{key}' with NO '?v=', so anything requesting "
            "an image without the query parameter (outbound LINE/Telegram "
            "messages, shared links, any surface not yet on UiImage) will 404 "
            "after pruning. Requires --yes-i-understand-this-is-irreversible."
        ),
    )
    parser.add_argument(
        "--yes-i-understand-this-is-irreversible",
        action="store_true",
        dest="prune_confirmed",
        help="Second, explicit consent required by --prune-originals.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
        help="Log verbosity on stderr (default %(default)s).",
    )
    return parser


@dataclass(slots=True)
class _RunState:
    """Totals kept *outside* the event loop.

    Ctrl-C during ``asyncio.run`` raises ``KeyboardInterrupt`` in the main
    thread, not inside the running coroutine — the coroutine is cancelled
    and its return value is lost. A multi-hour job that prints nothing
    when you stop it is a job you cannot stop, so the accumulator lives
    here and the report is written by :func:`main`.
    """

    accumulator: BackfillAccumulator
    dry_run: bool
    inventory: ObjectKeyInventory | None = None
    reportable: bool = False


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        stream=sys.stderr,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    error = _validate(args)
    if error:
        print(f"error: {error}", file=sys.stderr)
        return 2

    state = _RunState(accumulator=BackfillAccumulator(), dry_run=args.dry_run)
    try:
        exit_code = asyncio.run(_run(args, state))
    except KeyboardInterrupt:
        _LOGGER.warning("interrupted — re-run to resume where this left off")
        exit_code = 130
    if state.reportable:
        _report(state)
    return exit_code


def _validate(args: argparse.Namespace) -> str | None:
    if args.limit is not None and args.limit < 1:
        return "--limit must be >= 1"
    if not 1 <= args.concurrency <= MAX_CONCURRENCY:
        return f"--concurrency must be between 1 and {MAX_CONCURRENCY}"
    if args.progress_every < 1:
        return "--progress-every must be >= 1"
    if args.prune_originals and not args.prune_confirmed:
        return (
            "--prune-originals deletes the source images permanently and "
            "breaks every URL served without '?v='; pass "
            "--yes-i-understand-this-is-irreversible to proceed"
        )
    if args.prune_confirmed and not args.prune_originals:
        return (
            "--yes-i-understand-this-is-irreversible has no effect without "
            "--prune-originals"
        )
    return None


# --------------------------------------------------------------------------- #
# Execution
# --------------------------------------------------------------------------- #


async def _run(args: argparse.Namespace, state: _RunState) -> int:
    # Imported here, not at module scope: pulling in the container drags the
    # whole application graph, and `--help` should not pay for it (nor fail
    # on a machine without the production environment).
    from kokoro_link.bootstrap.container import _build_storage_adapter
    from kokoro_link.bootstrap.settings import AppSettings
    from kokoro_link.infrastructure.persistence.engine import (
        build_async_engine,
        build_session_factory,
    )

    try:
        settings = AppSettings.from_env()
    except ValueError as exc:
        print(f"error: invalid environment: {exc}", file=sys.stderr)
        return 2
    if not settings.database_url:
        print(
            "error: DATABASE_URL is not set — run this on the deployment "
            "host, with the app's environment loaded.",
            file=sys.stderr,
        )
        return 2

    # The RAW adapter, deliberately not the VariantAwareObjectStorage the
    # container wires up: this script writes variant keys itself, and
    # --prune-originals calls delete() on the original, which the decorator
    # would fan out into deleting the three variants we just produced.
    storage = _build_storage_adapter(settings)
    engine = build_async_engine(
        settings.database_url,
        # One connection is enough — the DB is scanned once, up front,
        # while the slow part (storage + encode) has its own budget.
        pool_size=1,
        max_overflow=1,
    )
    session_factory = build_session_factory(engine)

    if args.prune_originals:
        _LOGGER.warning(
            "--prune-originals is ON: originals will be DELETED once their "
            "variants exist. This cannot be undone.",
        )

    state.reportable = True
    executor = ThreadPoolExecutor(
        max_workers=args.concurrency, thread_name_prefix="variant-encode",
    )
    try:
        try:
            state.inventory = await build_inventory(
                iter_url_references(session_factory),
                resolve_key=storage.object_key_from_url,
                limit=args.limit,
            )
        except (SQLAlchemyError, OSError) as exc:
            # An unreachable database is an operator mistake (wrong host,
            # env not loaded, tunnel down), not a bug — say so in one line
            # instead of a 40-frame asyncpg traceback.
            _LOGGER.error("database scan failed: %s: %s", type(exc).__name__, exc)
            state.reportable = False
            return 2
        _log_inventory(state.inventory)
        await run_backfill(
            state.inventory.keys,
            storage=storage,
            encode=make_thread_encoder(executor),
            concurrency=args.concurrency,
            dry_run=args.dry_run,
            prune_originals=args.prune_originals,
            accumulator=state.accumulator,
            progress=_progress_line,
            progress_every=args.progress_every,
        )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        await engine.dispose()

    return 1 if state.accumulator.failed else 0


def _log_inventory(inventory: ObjectKeyInventory) -> None:
    _LOGGER.info(
        "inventory: %d unique object keys from %d references (%s); "
        "%d duplicates, %d unresolvable, %d already-variant%s",
        len(inventory.keys),
        inventory.references_scanned,
        ", ".join(
            f"{source}={count}"
            for source, count in inventory.references_by_source.items()
        )
        or "none",
        inventory.duplicates,
        inventory.unresolvable,
        inventory.derived_skipped,
        " (limit reached)" if inventory.truncated else "",
    )


def _progress_line(progress: BackfillProgress) -> None:
    stats = progress.stats
    _LOGGER.info(
        "[%d/%d] generated=%d skipped=%d missing=%d failed=%d variants=%d",
        progress.processed,
        progress.total,
        stats.generated,
        stats.skipped,
        stats.missing,
        stats.failed,
        stats.variants_written,
    )


def _report(state: _RunState) -> None:
    """Human summary on stderr, machine summary (JSON) on stdout."""
    accumulator = state.accumulator
    stats = accumulator.snapshot()
    for line in format_stats(stats, dry_run=state.dry_run):
        print(line, file=sys.stderr)
    for object_key, message in accumulator.errors[:_MAX_REPORTED_ERRORS]:
        print(f"  failed: {object_key}: {message}", file=sys.stderr)
    overflow = len(accumulator.errors) - _MAX_REPORTED_ERRORS
    if overflow > 0:
        print(f"  ... and {overflow} more failures", file=sys.stderr)

    payload: dict[str, object] = {"dry_run": state.dry_run, **stats.to_dict()}
    inventory = state.inventory
    if inventory is not None:
        payload["inventory"] = {
            "unique_keys": len(inventory.keys),
            "references_scanned": inventory.references_scanned,
            "references_by_source": dict(inventory.references_by_source),
            "duplicates": inventory.duplicates,
            "unresolvable": inventory.unresolvable,
            "derived_skipped": inventory.derived_skipped,
            "truncated": inventory.truncated,
        }
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
