"""Engine behind ``scripts/backfill_image_variants.py`` (plan IV3 / D10).

Split from the CLI so every decision that can be wrong — which rows count
as image references, how duplicates collapse, when an image may be
skipped, what "saved bytes" means, when an original may be pruned — is a
plain function under unit test instead of a branch inside ``__main__``.

- :mod:`.sources` — DB-side inventory (the *only* possible inventory:
  object storage has no list-by-prefix).
- :mod:`.runner`  — idempotent, resumable, bounded-concurrency execution
  over that inventory.
"""

from __future__ import annotations

from scripts.image_variant_backfill.runner import (
    DEFAULT_CONCURRENCY,
    DEFAULT_PROGRESS_EVERY,
    MAX_CONCURRENCY,
    BackfillAccumulator,
    BackfillProgress,
    BackfillStats,
    KeyOutcome,
    OutcomeStatus,
    backfill_object,
    format_stats,
    make_thread_encoder,
    run_backfill,
)
from scripts.image_variant_backfill.sources import (
    ALBUM_SOURCE,
    FEED_SOURCE,
    STAGE_SOURCE,
    ObjectKeyInventory,
    UrlReference,
    build_inventory,
    is_derived_variant_key,
    iter_url_references,
    parse_stage_image_urls,
)

__all__ = [
    "ALBUM_SOURCE",
    "DEFAULT_CONCURRENCY",
    "DEFAULT_PROGRESS_EVERY",
    "FEED_SOURCE",
    "MAX_CONCURRENCY",
    "STAGE_SOURCE",
    "BackfillAccumulator",
    "BackfillProgress",
    "BackfillStats",
    "KeyOutcome",
    "ObjectKeyInventory",
    "OutcomeStatus",
    "UrlReference",
    "backfill_object",
    "build_inventory",
    "format_stats",
    "is_derived_variant_key",
    "iter_url_references",
    "make_thread_encoder",
    "parse_stage_image_urls",
    "run_backfill",
]
