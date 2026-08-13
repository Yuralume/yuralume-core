"""IV3 — existing-stock WebP variant backfill.

Covers IV3: the DB
is the only possible inventory (and the same picture is referenced twice),
already-done work is skipped, one bad image does not end the batch,
``--dry-run`` writes nothing, and ``--prune-originals`` is off unless
asked for twice.

The storage double is the real :class:`InMemoryObjectStorage` and the
encoder is the real :func:`encode_variants` on real PNG bytes — the one
thing this script must get right is producing byte-identical keys and
bytes to what IV1 would have written, and a mocked encoder would test
nothing of the sort.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator, Mapping
from datetime import datetime, timezone

import pytest
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from kokoro_link.contracts.object_storage import ObjectStorageError
from kokoro_link.infrastructure.persistence.models import (
    CharacterAlbumItemRow,
    CharacterRow,
    FeedPostRow,
)
from kokoro_link.infrastructure.storage.http import HttpObjectStorage
from kokoro_link.infrastructure.storage.image_variants import (
    VARIANT_SPECS,
    EncodedVariant,
    derive_variant_key,
    encode_variants,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.storage.variant_aware import (
    VariantAwareObjectStorage,
)
from scripts.backfill_image_variants import build_parser, _validate
from scripts.image_variant_backfill.runner import (
    BackfillAccumulator,
    OutcomeStatus,
    backfill_object,
    make_thread_encoder,
    run_backfill,
)
from scripts.image_variant_backfill.sources import (
    ALBUM_SOURCE,
    FEED_SOURCE,
    STAGE_SOURCE,
    UrlReference,
    build_inventory,
    is_derived_variant_key,
    iter_url_references,
    parse_stage_image_urls,
)

_KEY_A = "characters/char-1/tools/a.png"
_KEY_B = "characters/char-1/stage/b.png"
_FEED_KEY = "feed/c1/f0f1.png"
"""Where ``feed_composer_service._write_image_bytes`` puts a LumeGram
picture — its own prefix, reachable from no other table (IV12)."""


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _png_bytes(width: int = 800, height: int = 1200) -> bytes:
    """A real, decodable PNG — the encoder actually runs on this."""
    image = Image.new("RGB", (width, height), color=(180, 60, 90))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def _encode(content: bytes) -> Mapping[str, EncodedVariant]:
    """Inline encoder: same function the CLI hands to a thread pool."""
    return encode_variants(content)


async def _refs(*references: UrlReference) -> AsyncIterator[UrlReference]:
    for reference in references:
        yield reference


async def _seed(
    storage: InMemoryObjectStorage,
    *keys: str,
    width: int = 800,
    height: int = 1200,
) -> None:
    for key in keys:
        await storage.put_bytes(
            object_key=key,
            content=_png_bytes(width, height),
            content_type="image/png",
            metadata={"character_id": "char-1"},
        )


async def _keys_in(storage: InMemoryObjectStorage) -> set[str]:
    # ``stat`` is the only public existence probe on the port.
    return {
        key
        for key in _ALL_PROBE_KEYS
        if await storage.stat(object_key=key) is not None
    }


_ALL_PROBE_KEYS = [
    key
    for original in (_KEY_A, _KEY_B)
    for key in (
        original,
        *(derive_variant_key(original, spec.name) for spec in VARIANT_SPECS),
    )
]


class _BrokenReadStorage(InMemoryObjectStorage):
    """Fails ``get_bytes`` for one key — the "one rotten image" case."""

    def __init__(self, *, poisoned: str) -> None:
        super().__init__()
        self._poisoned = poisoned

    async def get_bytes(self, *, object_key: str) -> bytes:
        if object_key == self._poisoned:
            raise ObjectStorageError("upstream exploded")
        return await super().get_bytes(object_key=object_key)


class _BrokenWriteStorage(InMemoryObjectStorage):
    """Fails writes of one variant tier across every image."""

    def __init__(self, *, poisoned_tier: str) -> None:
        super().__init__()
        self._suffix = f".{poisoned_tier}.webp"

    async def put_bytes(self, **kwargs):  # noqa: ANN003, ANN201
        if str(kwargs.get("object_key", "")).endswith(self._suffix):
            raise ObjectStorageError("variant write rejected")
        return await super().put_bytes(**kwargs)


# --------------------------------------------------------------------------- #
# sources — inventory, de-duplication, URL shapes
# --------------------------------------------------------------------------- #


def test_parse_stage_image_urls_is_tolerant() -> None:
    assert parse_stage_image_urls('["/v1/public/a.png", "/uploads/b.png"]') == (
        "/v1/public/a.png",
        "/uploads/b.png",
    )
    # A single malformed row must not be able to abort a whole backfill.
    assert parse_stage_image_urls(None) == ()
    assert parse_stage_image_urls("") == ()
    assert parse_stage_image_urls("not json") == ()
    assert parse_stage_image_urls('{"a": 1}') == ()
    assert parse_stage_image_urls('["ok", 3, null, "  "]') == ("ok",)


def test_object_key_from_url_handles_both_stored_url_shapes() -> None:
    """The two shapes the plan says legacy rows can carry."""
    storage = HttpObjectStorage(
        base_url="http://storage:8000",
        api_key="k",
        public_base_url="https://play.yuralume.com",
    )
    assert storage.object_key_from_url(f"/v1/public/{_KEY_A}") == _KEY_A
    assert storage.object_key_from_url(f"/uploads/{_KEY_A}") == _KEY_A
    assert (
        storage.object_key_from_url(
            f"https://play.yuralume.com/v1/public/{_KEY_A}",
        )
        == _KEY_A
    )
    assert storage.object_key_from_url("https://elsewhere.example/x.png") is None


@pytest.mark.asyncio
async def test_inventory_deduplicates_a_key_referenced_by_both_sources() -> None:
    """``promote_to_stage`` leaves the same file in both columns; without
    de-duplication it would be fetched and re-encoded twice."""
    storage = InMemoryObjectStorage()
    inventory = await build_inventory(
        _refs(
            UrlReference(ALBUM_SOURCE, f"/uploads/{_KEY_A}"),
            UrlReference(STAGE_SOURCE, f"/uploads/{_KEY_A}"),
            UrlReference(STAGE_SOURCE, f"/uploads/{_KEY_B}"),
        ),
        resolve_key=storage.object_key_from_url,
    )
    assert inventory.keys == (_KEY_A, _KEY_B)
    assert inventory.references_scanned == 3
    assert inventory.duplicates == 1
    assert inventory.references_by_source == {ALBUM_SOURCE: 1, STAGE_SOURCE: 2}


@pytest.mark.asyncio
async def test_inventory_drops_unresolvable_and_already_derived_keys() -> None:
    storage = InMemoryObjectStorage()
    inventory = await build_inventory(
        _refs(
            UrlReference(ALBUM_SOURCE, "https://cdn.example.com/foreign.png"),
            UrlReference(
                ALBUM_SOURCE,
                f"/uploads/{derive_variant_key(_KEY_A, 'full')}",
            ),
            UrlReference(ALBUM_SOURCE, f"/uploads/{_KEY_A}"),
        ),
        resolve_key=storage.object_key_from_url,
    )
    assert inventory.keys == (_KEY_A,)
    assert inventory.unresolvable == 1
    # Backfilling a variant would yield a.png.full.webp.w320.webp.
    assert inventory.derived_skipped == 1
    assert is_derived_variant_key(derive_variant_key(_KEY_A, "w320"))
    assert not is_derived_variant_key(_KEY_A)


@pytest.mark.asyncio
async def test_inventory_deduplicates_a_feed_post_reusing_an_album_image(
) -> None:
    """A *manual* feed post carries a caller-supplied ``image_url`` that
    is not restricted to the ``feed/`` prefix, so the third source can
    land on a picture the album already owns."""
    storage = InMemoryObjectStorage()
    inventory = await build_inventory(
        _refs(
            UrlReference(ALBUM_SOURCE, f"/uploads/{_KEY_A}"),
            UrlReference(FEED_SOURCE, f"/uploads/{_KEY_A}"),
            UrlReference(FEED_SOURCE, f"/uploads/{_FEED_KEY}"),
        ),
        resolve_key=storage.object_key_from_url,
    )
    # One fetch/decode/encode per picture, not per reference.
    assert inventory.keys == (_KEY_A, _FEED_KEY)
    assert inventory.duplicates == 1
    assert inventory.references_by_source == {ALBUM_SOURCE: 1, FEED_SOURCE: 2}


@pytest.mark.asyncio
async def test_enumeration_walks_all_three_tables_of_a_real_database() -> None:
    """The three source columns, read the way the script reads them.

    Exercises the actual streaming statements (the JSON decode of
    ``characters.image_urls`` and the null/blank guard on
    ``feed_posts.image_url``) rather than a hand-rolled row stub, on a
    throwaway SQLite database holding only the tables involved.
    """
    stage_key = "characters/c1/stage/s.png"
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as conn:
            for table in (
                CharacterRow.__table__,
                CharacterAlbumItemRow.__table__,
                FeedPostRow.__table__,
            ):
                await conn.run_sync(table.create)
        session_factory = sessionmaker(
            engine, class_=AsyncSession, expire_on_commit=False,
        )
        now = datetime.now(timezone.utc)
        async with session_factory() as session:
            session.add(
                CharacterRow(
                    id="c1",
                    user_id="op1",
                    name="c",
                    # Modern shape, plus the same picture the album holds.
                    image_urls=json.dumps(
                        [f"/v1/public/{stage_key}", f"/v1/public/{_KEY_A}"],
                    ),
                    created_at=now,
                ),
            )
            session.add(
                CharacterAlbumItemRow(
                    id="i1",
                    character_id="c1",
                    # Legacy shape.
                    url=f"/uploads/{_KEY_A}",
                    source="tool",
                    created_at=now,
                ),
            )
            for index, image_url in enumerate(
                (
                    f"/v1/public/{_FEED_KEY}",
                    # Manual post pointing back at the album's picture.
                    f"/uploads/{_KEY_A}",
                    # A text-only post, and two rows that are "not NULL"
                    # yet carry nothing a resolver could use.
                    None,
                    "",
                    "   ",
                ),
            ):
                session.add(
                    FeedPostRow(
                        id=f"p{index}",
                        character_id="c1",
                        kind="daily",
                        content_text="t",
                        source_kind="manual",
                        source_ref_id=f"r{index}",
                        image_url=image_url,
                        created_at=now,
                    ),
                )
            await session.commit()

        resolver = HttpObjectStorage(
            base_url="http://storage:8000",
            api_key="k",
            public_base_url="https://play.yuralume.com",
        )
        inventory = await build_inventory(
            iter_url_references(session_factory, batch_size=2),
            resolve_key=resolver.object_key_from_url,
        )
    finally:
        await engine.dispose()

    # The three blank posts contribute no references at all — counting
    # them would overstate how much stock the feed actually holds.
    assert inventory.references_by_source == {
        ALBUM_SOURCE: 1, FEED_SOURCE: 2, STAGE_SOURCE: 2,
    }
    # Album row first (it is scanned first), then the stage's new picture,
    # then the feed's own; the stage's and the feed's duplicate references
    # to the album picture both collapse.
    assert inventory.keys == (_KEY_A, stage_key, _FEED_KEY)
    assert inventory.duplicates == 2


@pytest.mark.asyncio
async def test_inventory_limit_caps_unique_keys_and_flags_truncation() -> None:
    storage = InMemoryObjectStorage()
    inventory = await build_inventory(
        _refs(
            UrlReference(ALBUM_SOURCE, f"/uploads/{_KEY_A}"),
            UrlReference(ALBUM_SOURCE, f"/uploads/{_KEY_B}"),
        ),
        resolve_key=storage.object_key_from_url,
        limit=1,
    )
    assert inventory.keys == (_KEY_A,)
    assert inventory.truncated is True


# --------------------------------------------------------------------------- #
# runner — the acceptance criteria
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_writes_the_same_keys_the_route_will_look_up() -> None:
    storage = InMemoryObjectStorage()
    await _seed(storage, _KEY_A)

    outcome = await backfill_object(_KEY_A, storage=storage, encode=_encode)

    assert outcome.status is OutcomeStatus.GENERATED
    assert outcome.variants_written == len(VARIANT_SPECS)
    for spec in VARIANT_SPECS:
        meta = await storage.stat(
            object_key=derive_variant_key(_KEY_A, spec.name),
        )
        assert meta is not None, spec.name
        assert meta.content_type == "image/webp"
    # Original untouched (plan D1 red line).
    assert await storage.stat(object_key=_KEY_A) is not None


@pytest.mark.asyncio
async def test_second_run_skips_everything() -> None:
    """Idempotency: a re-run is metadata probes and nothing else."""
    storage = InMemoryObjectStorage()
    await _seed(storage, _KEY_A, _KEY_B)

    first = await run_backfill(
        [_KEY_A, _KEY_B], storage=storage, encode=_encode, concurrency=2,
    )
    assert first.generated == 2
    assert first.skipped == 0

    after_first = await _keys_in(storage)
    second = await run_backfill(
        [_KEY_A, _KEY_B], storage=storage, encode=_encode, concurrency=2,
    )
    assert second.skipped == 2
    assert second.generated == 0
    assert second.variants_written == 0
    assert await _keys_in(storage) == after_first


@pytest.mark.asyncio
async def test_narrow_image_gets_only_full_and_still_resumes_as_skipped() -> None:
    """Never-upscale means "all three present" is unreachable for a small
    source; the resume check must not loop on it forever."""
    storage = InMemoryObjectStorage()
    await _seed(storage, _KEY_A, width=200, height=300)

    first = await backfill_object(_KEY_A, storage=storage, encode=_encode)
    assert first.status is OutcomeStatus.GENERATED
    assert first.variants_written == 1
    assert await storage.stat(
        object_key=derive_variant_key(_KEY_A, "w320"),
    ) is None

    second = await backfill_object(_KEY_A, storage=storage, encode=_encode)
    assert second.status is OutcomeStatus.SKIPPED
    assert second.variants_written == 0


@pytest.mark.asyncio
async def test_partially_written_image_heals_on_the_next_run() -> None:
    storage = InMemoryObjectStorage()
    await _seed(storage, _KEY_A)
    await backfill_object(_KEY_A, storage=storage, encode=_encode)
    await storage.delete(object_key=derive_variant_key(_KEY_A, "w768"))

    outcome = await backfill_object(_KEY_A, storage=storage, encode=_encode)

    assert outcome.status is OutcomeStatus.GENERATED
    assert outcome.variants_written == 1
    assert await storage.stat(
        object_key=derive_variant_key(_KEY_A, "w768"),
    ) is not None


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_but_still_measures() -> None:
    storage = InMemoryObjectStorage()
    await _seed(storage, _KEY_A, _KEY_B)
    before = await _keys_in(storage)

    stats = await run_backfill(
        [_KEY_A, _KEY_B],
        storage=storage,
        encode=_encode,
        concurrency=1,
        dry_run=True,
    )

    assert await _keys_in(storage) == before == {_KEY_A, _KEY_B}
    assert stats.generated == 2
    assert stats.variants_written == 2 * len(VARIANT_SPECS)
    # Numbers are measured, not guessed — a dry run really encodes.
    assert stats.variant_bytes > 0
    assert stats.saved_bytes > 0


@pytest.mark.asyncio
async def test_one_unreadable_image_does_not_stop_the_batch() -> None:
    storage = _BrokenReadStorage(poisoned=_KEY_A)
    await _seed(storage, _KEY_A, _KEY_B)

    stats = await run_backfill(
        [_KEY_A, _KEY_B], storage=storage, encode=_encode, concurrency=1,
    )

    assert stats.failed == 1
    assert stats.generated == 1
    assert stats.scanned == 2
    for spec in VARIANT_SPECS:
        assert await storage.stat(
            object_key=derive_variant_key(_KEY_B, spec.name),
        ) is not None


@pytest.mark.asyncio
async def test_a_rejected_variant_write_is_counted_not_raised() -> None:
    storage = _BrokenWriteStorage(poisoned_tier="w768")
    await _seed(storage, _KEY_A)

    outcome = await backfill_object(_KEY_A, storage=storage, encode=_encode)

    assert outcome.status is OutcomeStatus.FAILED
    # The tiers that *could* be written still were.
    assert outcome.variants_written == len(VARIANT_SPECS) - 1
    assert "w768" in outcome.error


@pytest.mark.asyncio
async def test_missing_object_and_undecodable_object_are_data_not_failures(
) -> None:
    storage = InMemoryObjectStorage()
    await storage.put_bytes(
        object_key=_KEY_B, content=b"not an image", content_type="image/png",
    )

    missing = await backfill_object(_KEY_A, storage=storage, encode=_encode)
    undecodable = await backfill_object(_KEY_B, storage=storage, encode=_encode)

    assert missing.status is OutcomeStatus.MISSING
    assert undecodable.status is OutcomeStatus.UNDECODABLE

    accumulator = BackfillAccumulator()
    accumulator.record(missing)
    accumulator.record(undecodable)
    assert accumulator.failed == 0
    assert accumulator.snapshot().missing == 1
    assert accumulator.snapshot().undecodable == 1


# --------------------------------------------------------------------------- #
# runner — pruning (irreversible, default off)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_originals_survive_by_default() -> None:
    storage = InMemoryObjectStorage()
    await _seed(storage, _KEY_A)

    stats = await run_backfill([_KEY_A], storage=storage, encode=_encode)

    assert stats.pruned == 0
    assert stats.reclaimed_bytes == 0
    assert await storage.stat(object_key=_KEY_A) is not None


def test_prune_needs_a_second_explicit_consent() -> None:
    parser = build_parser()
    assert parser.parse_args([]).prune_originals is False
    assert _validate(parser.parse_args([])) is None
    assert "irreversible" in (
        _validate(parser.parse_args(["--prune-originals"])) or ""
    )
    assert _validate(
        parser.parse_args(
            ["--prune-originals", "--yes-i-understand-this-is-irreversible"],
        ),
    ) is None


@pytest.mark.asyncio
async def test_prune_removes_the_original_and_keeps_every_variant() -> None:
    storage = InMemoryObjectStorage()
    await _seed(storage, _KEY_A)

    stats = await run_backfill(
        [_KEY_A], storage=storage, encode=_encode, prune_originals=True,
    )

    assert stats.pruned == 1
    assert stats.reclaimed_bytes > 0
    assert await storage.stat(object_key=_KEY_A) is None
    for spec in VARIANT_SPECS:
        assert await storage.stat(
            object_key=derive_variant_key(_KEY_A, spec.name),
        ) is not None


@pytest.mark.asyncio
async def test_prune_is_skipped_when_a_variant_write_failed() -> None:
    storage = _BrokenWriteStorage(poisoned_tier="w768")
    await _seed(storage, _KEY_A)

    outcome = await backfill_object(
        _KEY_A, storage=storage, encode=_encode, prune_originals=True,
    )

    assert outcome.pruned is False
    assert await storage.stat(object_key=_KEY_A) is not None


@pytest.mark.asyncio
async def test_dry_run_never_prunes() -> None:
    storage = InMemoryObjectStorage()
    await _seed(storage, _KEY_A)

    stats = await run_backfill(
        [_KEY_A],
        storage=storage,
        encode=_encode,
        dry_run=True,
        prune_originals=True,
    )

    assert stats.pruned == 1  # reported...
    assert await storage.stat(object_key=_KEY_A) is not None  # ...but not done


@pytest.mark.asyncio
async def test_going_through_the_decorator_would_erase_the_variants() -> None:
    """Why this script takes the raw adapter (runner module docstring).

    ``VariantAwareObjectStorage.delete`` fans out to the siblings — correct
    for every other caller, catastrophic for ``--prune-originals``.
    """
    storage = InMemoryObjectStorage()
    await _seed(storage, _KEY_A)
    await backfill_object(_KEY_A, storage=storage, encode=_encode)

    await VariantAwareObjectStorage(storage).delete(object_key=_KEY_A)

    assert await _keys_in(storage) == set()


# --------------------------------------------------------------------------- #
# runner — concurrency, progress, plumbing
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_concurrency_is_a_hard_ceiling_on_images_in_flight() -> None:
    storage = InMemoryObjectStorage()
    keys = [f"characters/char-1/tools/{index}.png" for index in range(8)]
    await _seed(storage, *keys, width=64, height=64)

    in_flight = 0
    peak = 0

    async def counting_encode(content: bytes) -> Mapping[str, EncodedVariant]:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await asyncio.sleep(0)
            return encode_variants(content)
        finally:
            in_flight -= 1

    stats = await run_backfill(
        keys, storage=storage, encode=counting_encode, concurrency=3,
    )

    assert stats.scanned == len(keys)
    assert peak <= 3


@pytest.mark.asyncio
async def test_progress_is_reported_while_the_batch_runs() -> None:
    storage = InMemoryObjectStorage()
    keys = [f"characters/char-1/tools/{index}.png" for index in range(5)]
    await _seed(storage, *keys, width=64, height=64)
    seen: list[tuple[int, int]] = []

    await run_backfill(
        keys,
        storage=storage,
        encode=_encode,
        concurrency=1,
        progress=lambda event: seen.append((event.processed, event.total)),
        progress_every=2,
    )

    assert (2, 5) in seen
    assert (4, 5) in seen
    assert seen[-1] == (5, 5)  # the tail is always reported


@pytest.mark.asyncio
async def test_thread_encoder_produces_the_same_variants_as_the_inline_one(
) -> None:
    content = _png_bytes(400, 600)
    threaded = await make_thread_encoder()(content)
    assert set(threaded) == set(encode_variants(content))


@pytest.mark.asyncio
async def test_empty_work_list_is_a_clean_no_op() -> None:
    stats = await run_backfill(
        [], storage=InMemoryObjectStorage(), encode=_encode,
    )
    assert stats.scanned == 0


def test_concurrency_and_limit_are_validated_before_any_io() -> None:
    parser = build_parser()
    assert "concurrency" in (
        _validate(parser.parse_args(["--concurrency", "0"])) or ""
    )
    assert "concurrency" in (
        _validate(parser.parse_args(["--concurrency", "99"])) or ""
    )
    assert "limit" in (_validate(parser.parse_args(["--limit", "0"])) or "")
