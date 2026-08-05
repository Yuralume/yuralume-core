"""Idempotent, resumable variant backfill over a fixed list of object keys.

This is the execution half of IV3. It is handed a de-duplicated work list
(see :mod:`.sources`) and an :class:`ObjectStoragePort` and brings each key
up to the same state ``VariantAwareObjectStorage`` (IV1) would have left it
in at write time — using the *same* ``VARIANT_SPECS`` and the *same*
``derive_variant_key``, because a key that differs by one byte from what
the route derives is a variant that is never found again.

Undecorated port, on purpose
----------------------------
The caller must pass the **raw** storage adapter, not the
``VariantAwareObjectStorage`` decorator the container wires up. Two
reasons, one of them load-bearing:

* ``--prune-originals`` calls ``delete`` on the original. The decorator's
  ``delete`` fans out to the three sibling variants — it would erase the
  renditions this script just spent hours producing, together with the
  original. That is not a hypothetical: it is the decorator's documented
  and correct behaviour for every other caller.
* ``put_bytes`` of a variant key only avoids a second-order fan-out
  (``a.png.full.webp.w320.webp``) because of the decorator's
  ``_is_derived_variant_key`` guard. Depending on a guard to accidentally
  do nothing is a worse contract than not going through the layer whose
  job we are doing by hand.

Idempotency and resume
----------------------
Both come from the same probe and no extra state (no marker table, no
cursor file — plan D10 forbids a migration and a cursor file cannot
survive a container replacement anyway):

1. ``stat`` the three variant keys. All present -> skip, no download.
2. Otherwise fetch the original and encode. The encoder decides which
   tiers *should* exist (it never upscales), so a 300 px avatar legitimately
   has only ``full`` and is "complete" with one variant.
3. Write only the tiers that are missing.

Step 2 means an image narrower than 768 px is re-fetched and re-encoded on
every run, because "all three present" can never be true for it. That is
the deliberate trade: the alternative is persisting a per-object "already
processed" marker, and this way a *partially* written image (variant 2 of 3
failed last run) heals itself on the next run instead of staying broken
forever. Narrow images are also the cheap ones to redo.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from concurrent.futures import Executor
from dataclasses import dataclass, field
from enum import Enum

from kokoro_link.contracts.object_storage import (
    ObjectNotFoundError,
    ObjectStoragePort,
)
from kokoro_link.infrastructure.storage.image_variants import (
    VARIANT_SPECS,
    EncodedVariant,
    derive_variant_key,
    encode_variants,
)

__all__ = [
    "DEFAULT_CONCURRENCY",
    "DEFAULT_PROGRESS_EVERY",
    "MAX_CONCURRENCY",
    "BackfillAccumulator",
    "BackfillProgress",
    "BackfillStats",
    "EncodeCallable",
    "KeyOutcome",
    "OutcomeStatus",
    "backfill_object",
    "format_stats",
    "make_thread_encoder",
    "run_backfill",
]

_LOGGER = logging.getLogger("backfill_image_variants")

DEFAULT_CONCURRENCY = 3
"""Images in flight at once.

Each slot holds, at peak: the original bytes (~2.3 MB for the 1024x1536
PNGs this deployment generates), Pillow's decoded bitmap (~6 MB), the
LANCZOS-resized intermediates and three encoded WebP buffers — call it
~40 MB. Three slots is ~120 MB, which a small OCI VPS can spare *while
still serving traffic*; this script is expected to run against a live
host, so the default is chosen to be boring rather than fast."""

MAX_CONCURRENCY = 16
"""Hard ceiling on ``--concurrency``. Past this the encoder thread pool
starts competing with the API workers for the same handful of vCPUs and
the box gets slower overall, not faster."""

DEFAULT_PROGRESS_EVERY = 25

# ``bytes -> {tier name: EncodedVariant}``. Injected rather than imported
# at the call site so the CLI can bind it to a bounded thread pool and
# tests can substitute a stub that fails on demand.
EncodeCallable = Callable[[bytes], Awaitable[Mapping[str, EncodedVariant]]]


class OutcomeStatus(str, Enum):
    """What happened to one object key."""

    SKIPPED = "skipped"
    """Every tier the encoder would produce was already in storage."""
    GENERATED = "generated"
    """At least one tier was missing and is now (or, in a dry run, would
    be) present."""
    MISSING = "missing"
    """A DB row points at an object that is not in storage. Normal for
    legacy rows and for anything already pruned — data, not a fault."""
    UNDECODABLE = "undecodable"
    """The object exists but is not an image Pillow can read. Also data,
    not a fault: the columns are ``Text``, nothing enforces image-ness."""
    FAILED = "failed"
    """Storage or encoder error. Counted, logged, and stepped over."""


@dataclass(frozen=True, slots=True)
class KeyOutcome:
    object_key: str
    status: OutcomeStatus
    variants_written: int = 0
    original_bytes: int = 0
    variant_bytes: int = 0
    saved_bytes: int = 0
    pruned: bool = False
    reclaimed_bytes: int = 0
    error: str = ""


@dataclass(frozen=True, slots=True)
class BackfillStats:
    """Terminal report (plan D10: scanned / generated / skipped / failed /
    saved bytes) plus the buckets needed to read those five honestly."""

    scanned: int = 0
    generated: int = 0
    skipped: int = 0
    missing: int = 0
    undecodable: int = 0
    failed: int = 0
    variants_written: int = 0
    pruned: int = 0
    original_bytes: int = 0
    variant_bytes: int = 0
    saved_bytes: int = 0
    reclaimed_bytes: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "scanned": self.scanned,
            "generated": self.generated,
            "skipped": self.skipped,
            "missing": self.missing,
            "undecodable": self.undecodable,
            "failed": self.failed,
            "variants_written": self.variants_written,
            "pruned": self.pruned,
            "original_bytes": self.original_bytes,
            "variant_bytes": self.variant_bytes,
            "saved_bytes": self.saved_bytes,
            "reclaimed_bytes": self.reclaimed_bytes,
        }


@dataclass(frozen=True, slots=True)
class BackfillProgress:
    processed: int
    total: int
    stats: BackfillStats


@dataclass(slots=True)
class BackfillAccumulator:
    """Running totals, owned by the caller.

    Deliberately *not* internal to :func:`run_backfill`: a run that is
    interrupted (Ctrl-C on a multi-hour job, or the VPS ssh session
    dropping) still has to print what it achieved, and it can only do that
    if the totals live outside the coroutine that was cancelled.
    """

    scanned: int = 0
    generated: int = 0
    skipped: int = 0
    missing: int = 0
    undecodable: int = 0
    failed: int = 0
    variants_written: int = 0
    pruned: int = 0
    original_bytes: int = 0
    variant_bytes: int = 0
    saved_bytes: int = 0
    reclaimed_bytes: int = 0
    errors: list[tuple[str, str]] = field(default_factory=list)

    def record(self, outcome: KeyOutcome) -> None:
        self.scanned += 1
        if outcome.status is OutcomeStatus.SKIPPED:
            self.skipped += 1
        elif outcome.status is OutcomeStatus.GENERATED:
            self.generated += 1
        elif outcome.status is OutcomeStatus.MISSING:
            self.missing += 1
        elif outcome.status is OutcomeStatus.UNDECODABLE:
            self.undecodable += 1
        else:
            self.failed += 1
            if outcome.error:
                self.errors.append((outcome.object_key, outcome.error))
        self.variants_written += outcome.variants_written
        self.original_bytes += outcome.original_bytes
        self.variant_bytes += outcome.variant_bytes
        self.saved_bytes += outcome.saved_bytes
        self.reclaimed_bytes += outcome.reclaimed_bytes
        if outcome.pruned:
            self.pruned += 1

    def snapshot(self) -> BackfillStats:
        return BackfillStats(
            scanned=self.scanned,
            generated=self.generated,
            skipped=self.skipped,
            missing=self.missing,
            undecodable=self.undecodable,
            failed=self.failed,
            variants_written=self.variants_written,
            pruned=self.pruned,
            original_bytes=self.original_bytes,
            variant_bytes=self.variant_bytes,
            saved_bytes=self.saved_bytes,
            reclaimed_bytes=self.reclaimed_bytes,
        )


def make_thread_encoder(executor: Executor | None = None) -> EncodeCallable:
    """Bind :func:`encode_variants` to ``executor``.

    Pillow's WebP encode is CPU-bound native work measured in hundreds of
    milliseconds per 1024x1536 source. Run inline it would block the event
    loop and serialise the whole batch behind one image at a time; handed
    to a pool sized to ``--concurrency`` it overlaps with the next image's
    download while still holding a hard ceiling on how many cores this
    script can take from the API processes sharing the box.
    """

    async def _encode(content: bytes) -> Mapping[str, EncodedVariant]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(executor, encode_variants, content)

    return _encode


async def backfill_object(
    object_key: str,
    *,
    storage: ObjectStoragePort,
    encode: EncodeCallable,
    dry_run: bool = False,
    prune_originals: bool = False,
) -> KeyOutcome:
    """Bring one object key up to full variant coverage.

    Never raises: every failure mode is folded into a
    :class:`KeyOutcome`, because "one bad image must not end the batch" is
    an acceptance criterion, and a batch of several thousand images run
    unattended over ssh will find every failure mode there is.
    """
    try:
        return await _backfill_object(
            object_key,
            storage=storage,
            encode=encode,
            dry_run=dry_run,
            prune_originals=prune_originals,
        )
    except ObjectNotFoundError:
        # Raced with a delete between the DB scan and the fetch, or the
        # row outlived its object. Same meaning as a ``stat`` miss.
        return KeyOutcome(object_key=object_key, status=OutcomeStatus.MISSING)
    except Exception as exc:  # noqa: BLE001 — see docstring
        _LOGGER.warning(
            "backfill failed for %s: %s", object_key, exc, exc_info=True,
        )
        return KeyOutcome(
            object_key=object_key,
            status=OutcomeStatus.FAILED,
            error=f"{type(exc).__name__}: {exc}",
        )


async def _backfill_object(
    object_key: str,
    *,
    storage: ObjectStoragePort,
    encode: EncodeCallable,
    dry_run: bool,
    prune_originals: bool,
) -> KeyOutcome:
    present = await _present_variants(storage, object_key)
    if len(present) == len(VARIANT_SPECS):
        # Fast path, and the only path most keys take on a resumed run:
        # three metadata probes, no download, no decode.
        return KeyOutcome(object_key=object_key, status=OutcomeStatus.SKIPPED)

    metadata = await storage.stat(object_key=object_key)
    if metadata is None:
        return KeyOutcome(object_key=object_key, status=OutcomeStatus.MISSING)

    content = await storage.get_bytes(object_key=object_key)
    variants = await encode(content)
    if not variants:
        return KeyOutcome(
            object_key=object_key,
            status=OutcomeStatus.UNDECODABLE,
            original_bytes=metadata.size_bytes,
        )

    expected = set(variants)
    outstanding = [
        variant for name, variant in variants.items() if name not in present
    ]
    if not outstanding:
        # Every tier this source *can* have already exists — the
        # never-upscale rule, not an error.
        return KeyOutcome(object_key=object_key, status=OutcomeStatus.SKIPPED)

    written = 0
    written_bytes = 0
    write_errors: list[str] = []
    for variant in outstanding:
        variant_key = derive_variant_key(object_key, variant.name)
        if dry_run:
            written += 1
            written_bytes += len(variant.content)
            continue
        try:
            await storage.put_bytes(
                object_key=variant_key,
                content=variant.content,
                content_type=variant.content_type,
                # Mirror the original's metadata so a backfilled variant is
                # indistinguishable from one IV1 wrote at generation time.
                metadata=dict(metadata.metadata or {}),
            )
        except Exception as exc:  # noqa: BLE001 — per-variant fail-soft
            _LOGGER.warning(
                "failed to store backfilled variant %s: %s", variant_key, exc,
            )
            write_errors.append(f"{variant.name}: {exc}")
            continue
        written += 1
        written_bytes += len(variant.content)

    complete = write_errors == [] and expected <= (
        present | {variant.name for variant in outstanding}
    )
    pruned, reclaimed = await _maybe_prune(
        object_key,
        storage=storage,
        prune_originals=prune_originals,
        dry_run=dry_run,
        complete=complete,
        has_full_tier="full" in expected,
        original_bytes=metadata.size_bytes,
    )

    full = variants.get("full")
    saved = (
        max(0, metadata.size_bytes - len(full.content)) if full is not None else 0
    )
    return KeyOutcome(
        object_key=object_key,
        status=(
            OutcomeStatus.GENERATED if not write_errors else OutcomeStatus.FAILED
        ),
        variants_written=written,
        original_bytes=metadata.size_bytes,
        variant_bytes=written_bytes,
        saved_bytes=saved,
        pruned=pruned,
        reclaimed_bytes=reclaimed,
        error="; ".join(write_errors),
    )


async def _present_variants(
    storage: ObjectStoragePort, object_key: str,
) -> set[str]:
    """Which tiers already exist, probed concurrently (one round trip)."""
    names = [spec.name for spec in VARIANT_SPECS]
    results = await asyncio.gather(
        *(
            storage.stat(object_key=derive_variant_key(object_key, name))
            for name in names
        ),
    )
    return {
        name
        for name, meta in zip(names, results, strict=True)
        if meta is not None
    }


async def _maybe_prune(
    object_key: str,
    *,
    storage: ObjectStoragePort,
    prune_originals: bool,
    dry_run: bool,
    complete: bool,
    has_full_tier: bool,
    original_bytes: int,
) -> tuple[bool, int]:
    """Delete the original — only when it is provably replaceable.

    Two gates, both non-negotiable: the full set of tiers this source can
    have must now exist (``complete``), and one of them must be ``full``
    (the only tier that preserves the original pixel dimensions). Without
    ``full`` present, deleting the original is not a space optimisation,
    it is losing the picture.
    """
    if not prune_originals or not complete or not has_full_tier:
        return False, 0
    if dry_run:
        return True, original_bytes
    await storage.delete(object_key=object_key)
    _LOGGER.info("pruned original %s (%d bytes)", object_key, original_bytes)
    return True, original_bytes


async def run_backfill(
    object_keys: Sequence[str],
    *,
    storage: ObjectStoragePort,
    encode: EncodeCallable,
    concurrency: int = DEFAULT_CONCURRENCY,
    dry_run: bool = False,
    prune_originals: bool = False,
    accumulator: BackfillAccumulator | None = None,
    progress: Callable[[BackfillProgress], None] | None = None,
    progress_every: int = DEFAULT_PROGRESS_EVERY,
) -> BackfillStats:
    """Process ``object_keys`` with at most ``concurrency`` images in flight.

    A fixed pool of workers pulling from one iterator, rather than
    ``gather`` over a semaphore: the peak resource footprint is then a
    property of the pool size alone and does not scale with the length of
    the work list, which is the whole point of a bounded batch job.
    """
    if concurrency < 1:
        raise ValueError("concurrency must be >= 1")
    totals = accumulator if accumulator is not None else BackfillAccumulator()
    total = len(object_keys)
    if total == 0:
        return totals.snapshot()

    pending = iter(object_keys)
    step = max(1, int(progress_every))

    async def worker() -> None:
        # ``next()`` on a list iterator is synchronous, so the event loop
        # cannot interleave two workers mid-step: no lock needed, and no
        # key can be handed out twice.
        for object_key in pending:
            outcome = await backfill_object(
                object_key,
                storage=storage,
                encode=encode,
                dry_run=dry_run,
                prune_originals=prune_originals,
            )
            totals.record(outcome)
            if progress is not None and (
                totals.scanned % step == 0 or totals.scanned == total
            ):
                progress(
                    BackfillProgress(
                        processed=totals.scanned,
                        total=total,
                        stats=totals.snapshot(),
                    ),
                )

    await asyncio.gather(
        *(worker() for _ in range(min(concurrency, total))),
    )
    return totals.snapshot()


def format_stats(stats: BackfillStats, *, dry_run: bool) -> Iterable[str]:
    """Human-readable summary lines (the JSON blob is the machine copy)."""
    verb = "would generate" if dry_run else "generated"
    yield f"scanned            {stats.scanned}"
    yield f"{verb:<18} {stats.generated} images / {stats.variants_written} variants"
    yield f"skipped (complete) {stats.skipped}"
    yield f"missing objects    {stats.missing}"
    yield f"undecodable        {stats.undecodable}"
    yield f"failed             {stats.failed}"
    yield f"original bytes     {_mib(stats.original_bytes)}"
    yield f"variant bytes      {_mib(stats.variant_bytes)}"
    yield (
        f"saved bytes        {_mib(stats.saved_bytes)} "
        "(original - full-tier WebP, per delivery)"
    )
    if stats.pruned or stats.reclaimed_bytes:
        pruned_verb = "would prune" if dry_run else "pruned"
        yield (
            f"{pruned_verb:<18} {stats.pruned} originals "
            f"/ {_mib(stats.reclaimed_bytes)} reclaimed"
        )


def _mib(value: int) -> str:
    return f"{value / (1024 * 1024):.1f} MiB ({value} B)"
