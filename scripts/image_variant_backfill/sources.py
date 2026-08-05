"""DB-side inventory of the image objects the product still references (IV3).

Object storage exposes no list-by-prefix call
(``contracts/object_storage.py`` — deliberately, see plan §1.7), so the
backfill cannot ask the bucket what it holds. The database is the only
inventory, and three columns hold every live image reference:

``character_album_items.url``
    One row per archived image. Also covers **chat-bubble images**: the
    ComfyUI image tool writes the very same URL onto the message
    attachment *and* into the album (``album_service.add_auto``), and the
    album cap GC deletes the stored object together with the row. So a
    chat image is either in this table or already gone — there is no
    other table to walk for chat.

``characters.image_urls``
    The stage carousel, a JSON-encoded list on the character row.

``feed_posts.image_url``
    The LumeGram post picture (IV12). An **independent** source, not a
    view of the other two: ``feed_composer_service._maybe_generate_image``
    writes its own object under ``feed/{character_id}/{uuid}.png`` and
    stores the URL only on the post row — it never calls
    ``album_service.add_auto`` (the album's sole writer is the ComfyUI
    tool), so nothing else in the database knows this key exists. Without
    this column the whole feed's existing stock stays un-backfilled.

    Nullable, and text-only posts are the common case, so the scan filters
    ``IS NOT NULL`` in SQL and still guards against a blank string in
    Python — ``''`` would resolve to no key and be counted as a reference
    that never existed.

    ``feed_posts.video_url`` is deliberately *not* a source: the variant
    encoder produces WebP renditions of still images, and handing it an
    MP4 would be an undecodable-object failure per row.

De-duplication is not an optimisation here. The same key routinely
appears in both album and stage (``promote_to_stage`` /
``transfer_from_stage`` move an image between the two without touching
the file), and a **manual** feed post takes a caller-supplied
``image_url`` (``POST /characters/{id}/feed/posts``) that is not
restricted to the ``feed/`` prefix — so an operator-authored post can
point at an album or stage picture too. Without de-duplication those
would be fetched, decoded and re-encoded once per reference.

Everything in this module is either a pure function or a thin streaming
query, so the interesting decisions stay unit-testable without a database.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterable, AsyncIterator, Callable, Mapping
from dataclasses import dataclass

from sqlalchemy import select

from kokoro_link.infrastructure.persistence.models import (
    CharacterAlbumItemRow,
    CharacterRow,
    FeedPostRow,
)
from kokoro_link.infrastructure.storage.image_variants import (
    VARIANT_SPECS,
    derive_variant_key,
)

__all__ = [
    "ALBUM_SOURCE",
    "FEED_SOURCE",
    "STAGE_SOURCE",
    "ObjectKeyInventory",
    "UrlReference",
    "build_inventory",
    "is_derived_variant_key",
    "iter_url_references",
    "parse_stage_image_urls",
]

ALBUM_SOURCE = "album"
STAGE_SOURCE = "stage"
FEED_SOURCE = "feed"

_DEFAULT_BATCH_SIZE = 500

# Derived from the one function allowed to know the variant key shape, the
# same way ``variant_aware.py`` derives its own copy. Two callers of a
# single authority cannot drift; restating ``".w320.webp"`` as a literal
# would be the version that can.
_SUFFIX_PROBE = "k"
_DERIVED_KEY_SUFFIXES: tuple[str, ...] = tuple(
    derive_variant_key(_SUFFIX_PROBE, spec.name)[len(_SUFFIX_PROBE):]
    for spec in VARIANT_SPECS
)


@dataclass(frozen=True, slots=True)
class UrlReference:
    """One media URL as it appears in one DB column."""

    source: str
    url: str


@dataclass(frozen=True, slots=True)
class ObjectKeyInventory:
    """De-duplicated work list plus the counters that explain how it shrank."""

    keys: tuple[str, ...]
    references_scanned: int
    references_by_source: Mapping[str, int]
    duplicates: int
    """References that resolved to a key already in ``keys`` — the
    stage/album overlap, mostly."""
    unresolvable: int
    """URLs the storage adapter could not reverse into an object key
    (foreign origins, hand-edited rows, keys with illegal segments)."""
    derived_skipped: int
    """References that already point *at a variant*. Backfilling one would
    produce ``a.png.full.webp.w320.webp``; ``variant_aware.py`` carries the
    same guard on the write side."""
    truncated: bool
    """``--limit`` stopped the scan before the tables were exhausted."""


def is_derived_variant_key(object_key: str) -> bool:
    """True when ``object_key`` is itself one of our WebP renditions."""
    return any(object_key.endswith(suffix) for suffix in _DERIVED_KEY_SUFFIXES)


def parse_stage_image_urls(raw: str | None) -> tuple[str, ...]:
    """Read ``characters.image_urls`` (JSON text) as tolerantly as possible.

    A backfill that dies on one malformed row leaves the whole deployment
    un-backfilled, so anything that is not a JSON list of non-empty
    strings simply contributes no URLs.
    """
    if not raw:
        return ()
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(
        entry.strip()
        for entry in parsed
        if isinstance(entry, str) and entry.strip()
    )


async def iter_url_references(
    session_factory: Callable[[], object],
    *,
    batch_size: int = _DEFAULT_BATCH_SIZE,
) -> AsyncIterator[UrlReference]:
    """Stream every image URL held in the three source columns.

    Server-side cursors (``session.stream`` + ``yield_per``) rather than a
    materialised list: this runs against the live production database while
    the app is serving, and the row count grows with every generated image.

    Ordering is explicit and stable so that a ``--limit``ed scan always
    samples the same prefix — an unordered scan would make two dry runs
    disagree for no reason. The source order (album, stage, feed) is part
    of that reproducibility: appending the feed last keeps an existing
    ``--limit N`` sampling the same images it sampled before.
    """
    async with session_factory() as session:  # type: ignore[attr-defined]
        album = await session.stream(
            select(CharacterAlbumItemRow.url)
            .order_by(
                CharacterAlbumItemRow.created_at,
                CharacterAlbumItemRow.id,
            )
            .execution_options(yield_per=batch_size),
        )
        async for (url,) in album:
            if url:
                yield UrlReference(source=ALBUM_SOURCE, url=url)

        stage = await session.stream(
            select(CharacterRow.image_urls)
            .order_by(CharacterRow.id)
            .execution_options(yield_per=batch_size),
        )
        async for (raw,) in stage:
            for url in parse_stage_image_urls(raw):
                yield UrlReference(source=STAGE_SOURCE, url=url)

        # Text-only posts are the majority, so the null filter belongs in
        # SQL: it keeps the cursor off rows that could never contribute a
        # reference. The Python guard below still has work to do — a blank
        # or whitespace-only string is not NULL, and counting it as a
        # scanned reference would overstate the feed's stock.
        feed = await session.stream(
            select(FeedPostRow.image_url)
            .where(FeedPostRow.image_url.is_not(None))
            .order_by(FeedPostRow.created_at, FeedPostRow.id)
            .execution_options(yield_per=batch_size),
        )
        async for (raw,) in feed:
            url = (raw or "").strip()
            if url:
                yield UrlReference(source=FEED_SOURCE, url=url)


async def build_inventory(
    references: AsyncIterable[UrlReference],
    *,
    resolve_key: Callable[[str], str | None],
    limit: int | None = None,
) -> ObjectKeyInventory:
    """Collapse a stream of URL references into a unique work list.

    ``resolve_key`` is the storage adapter's ``object_key_from_url`` — the
    one component that knows every URL shape this deployment has ever
    written (``/v1/public/{key}``, the legacy ``/uploads/{key}``, and both
    with the public base URL in front).

    ``limit`` caps **unique keys**, not raw references: ``--limit 20`` is
    meant to sample twenty images, and a cap on references would silently
    sample fewer whenever the stage and album overlap. Resuming a full run
    does not need ``limit`` at all — the runner's already-has-variants skip
    is what makes an interrupted run cheap to repeat.
    """
    keys: list[str] = []
    seen: set[str] = set()
    by_source: dict[str, int] = {}
    scanned = 0
    duplicates = 0
    unresolvable = 0
    derived_skipped = 0
    truncated = False

    try:
        async for reference in references:
            if limit is not None and len(keys) >= limit:
                truncated = True
                break
            scanned += 1
            by_source[reference.source] = by_source.get(reference.source, 0) + 1
            key = resolve_key(reference.url)
            if not key:
                unresolvable += 1
                continue
            if is_derived_variant_key(key):
                derived_skipped += 1
                continue
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            keys.append(key)
    finally:
        # ``--limit`` breaks out mid-stream, leaving the async generator
        # suspended *inside* its ``async with session`` — and therefore
        # holding a DB connection until the GC gets around to it. Close it
        # deterministically instead.
        aclose = getattr(references, "aclose", None)
        if aclose is not None:
            await aclose()

    return ObjectKeyInventory(
        keys=tuple(keys),
        references_scanned=scanned,
        references_by_source=dict(sorted(by_source.items())),
        duplicates=duplicates,
        unresolvable=unresolvable,
        derived_skipped=derived_skipped,
        truncated=truncated,
    )
