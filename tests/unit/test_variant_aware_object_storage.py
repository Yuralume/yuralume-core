"""IV1 — ``VariantAwareObjectStorage`` write/copy/delete fan-out.

The decorator's whole value is that no producer can skip it, so the tests
below drive it through the port surface rather than through any single
caller, and the last section drives it through the real public-media
route (the one consumer that reaches past ``ObjectStoragePort`` into an
optional capability).
"""

from __future__ import annotations

import asyncio
import io
import logging
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from kokoro_link.api.routes.public_objects import router
from kokoro_link.contracts.object_storage import (
    DEFAULT_STREAM_CHUNK_BYTES,
    EPHEMERAL_OBJECT_KEY_PREFIXES,
    ObjectStorageError,
)
from kokoro_link.infrastructure.storage.image_variants import (
    VARIANT_SPECS,
    derive_variant_key,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.storage.variant_aware import (
    VariantAwareObjectStorage,
)

_ORIGINAL_KEY = "characters/char-1/stage/a.png"


def _png_bytes(*, width: int = 800, height: int = 1200) -> bytes:
    """A real decodable PNG — the encoder must actually run on it."""
    image = Image.new("RGB", (width, height), color=(200, 40, 60))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _variant_keys(object_key: str) -> list[str]:
    return [derive_variant_key(object_key, spec.name) for spec in VARIANT_SPECS]


class _RecordingStorage(InMemoryObjectStorage):
    """In-memory adapter that records which keys each method saw."""

    def __init__(self, **kwargs) -> None:  # noqa: ANN003
        super().__init__(**kwargs)
        self.put_keys: list[str] = []
        self.delete_keys: list[str] = []
        self.copy_pairs: list[tuple[str, str]] = []
        self.iter_bytes_keys: list[str] = []
        self.get_bytes_keys: list[str] = []

    async def put_bytes(self, **kwargs):  # noqa: ANN003, ANN201
        self.put_keys.append(kwargs["object_key"])
        return await super().put_bytes(**kwargs)

    async def delete(self, *, object_key: str) -> None:
        self.delete_keys.append(object_key)
        await super().delete(object_key=object_key)

    async def copy(self, **kwargs):  # noqa: ANN003, ANN201
        self.copy_pairs.append((kwargs["source_key"], kwargs["destination_key"]))
        return await super().copy(**kwargs)

    async def get_bytes(self, *, object_key: str) -> bytes:
        self.get_bytes_keys.append(object_key)
        return await super().get_bytes(object_key=object_key)

    async def iter_bytes(  # noqa: ANN201
        self,
        *,
        object_key: str,
        chunk_size: int = DEFAULT_STREAM_CHUNK_BYTES,
    ):
        self.iter_bytes_keys.append(object_key)
        data = await InMemoryObjectStorage.get_bytes(self, object_key=object_key)
        step = max(1, int(chunk_size))
        for start in range(0, len(data), step):
            yield data[start:start + step]


# ---------------------------------------------------------------------------
# put_bytes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_put_image_writes_three_webp_siblings() -> None:
    inner = InMemoryObjectStorage()
    storage = VariantAwareObjectStorage(inner)
    payload = _png_bytes()

    stored = await storage.put_bytes(
        object_key=_ORIGINAL_KEY,
        content=payload,
        content_type="image/png",
    )

    assert stored.object_key == _ORIGINAL_KEY
    assert await inner.get_bytes(object_key=_ORIGINAL_KEY) == payload
    for variant_key in _variant_keys(_ORIGINAL_KEY):
        meta = await inner.stat(object_key=variant_key)
        assert meta is not None, variant_key
        assert meta.content_type == "image/webp"
        assert meta.size_bytes > 0


@pytest.mark.asyncio
async def test_variant_keys_stay_inside_the_object_key_charset() -> None:
    """D1: derived keys must survive ``validate_object_key`` unchanged.

    They already do (the storage write below would have raised otherwise);
    this pins it so a future rename of a tier cannot introduce a segment
    the key validator rejects.
    """
    inner = InMemoryObjectStorage()
    storage = VariantAwareObjectStorage(inner)

    await storage.put_bytes(
        object_key=_ORIGINAL_KEY,
        content=_png_bytes(),
        content_type="image/png",
    )

    for variant_key in _variant_keys(_ORIGINAL_KEY):
        assert variant_key.startswith(_ORIGINAL_KEY + ".")
        assert variant_key.endswith(".webp")
        assert await inner.stat(object_key=variant_key) is not None


@pytest.mark.asyncio
async def test_put_image_propagates_metadata_to_variants() -> None:
    inner = InMemoryObjectStorage()
    storage = VariantAwareObjectStorage(inner)

    await storage.put_bytes(
        object_key=_ORIGINAL_KEY,
        content=_png_bytes(),
        content_type="image/png",
        metadata={"character_id": "char-1", "kind": "stage"},
    )

    meta = await inner.stat(
        object_key=derive_variant_key(_ORIGINAL_KEY, "w320"),
    )
    assert meta is not None
    assert meta.metadata == {"character_id": "char-1", "kind": "stage"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content_type",
    ["audio/wav", "application/json", "text/plain", ""],
)
async def test_put_non_image_is_pure_pass_through(content_type: str) -> None:
    inner = _RecordingStorage()
    storage = VariantAwareObjectStorage(inner)

    await storage.put_bytes(
        object_key="tts/char-1/line.wav",
        content=_png_bytes(),  # image bytes, but not declared as an image
        content_type=content_type,
    )

    assert inner.put_keys == ["tts/char-1/line.wav"]


@pytest.mark.asyncio
async def test_content_type_parameters_still_count_as_image() -> None:
    inner = InMemoryObjectStorage()
    storage = VariantAwareObjectStorage(inner)

    await storage.put_bytes(
        object_key=_ORIGINAL_KEY,
        content=_png_bytes(),
        content_type="image/png; charset=binary",
    )

    assert await inner.stat(
        object_key=derive_variant_key(_ORIGINAL_KEY, "w320"),
    ) is not None


@pytest.mark.asyncio
async def test_never_upscales_so_narrow_sources_get_fewer_variants() -> None:
    """IV0 skips tiers wider than the source; IV1 must not invent them."""
    inner = InMemoryObjectStorage()
    storage = VariantAwareObjectStorage(inner)

    await storage.put_bytes(
        object_key=_ORIGINAL_KEY,
        content=_png_bytes(width=200, height=300),
        content_type="image/png",
    )

    assert await inner.stat(
        object_key=derive_variant_key(_ORIGINAL_KEY, "full"),
    ) is not None
    for name in ("w320", "w768"):
        assert await inner.stat(
            object_key=derive_variant_key(_ORIGINAL_KEY, name),
        ) is None, name


@pytest.mark.asyncio
async def test_re_putting_a_variant_key_does_not_cascade() -> None:
    """The IV3 backfill writes variant keys directly; wrapping must not
    turn that into ``...full.webp.w320.webp``."""
    inner = _RecordingStorage()
    storage = VariantAwareObjectStorage(inner)
    variant_key = derive_variant_key(_ORIGINAL_KEY, "full")

    await storage.put_bytes(
        object_key=variant_key,
        content=_png_bytes(),
        content_type="image/webp",
    )

    assert inner.put_keys == [variant_key]


@pytest.mark.asyncio
async def test_ephemeral_key_put_is_pure_pass_through() -> None:
    """Staged scratch space is never rendered, so variants are dead weight.

    And it is dead weight in the worst place: this decorator encodes and
    uploads synchronously, so three WebP encodes plus three uploads would land
    inside the foreground latency of a charged action.
    """
    inner = _RecordingStorage()
    storage = VariantAwareObjectStorage(inner)
    key = f"{EPHEMERAL_OBJECT_KEY_PREFIXES[0]}u-1/{uuid4().hex}.png"

    stored = await storage.put_bytes(
        object_key=key,
        content=_png_bytes(),  # a real, encodable image — only the key opts out
        content_type="image/png",
    )

    assert stored.object_key == key
    assert inner.put_keys == [key]


@pytest.mark.asyncio
async def test_ephemeral_key_delete_does_not_probe_for_variants() -> None:
    """The counterpart to the write side, and free because of it.

    ``delete`` is otherwise deliberately unconditional (an orphaned variant
    keeps serving a deleted picture), but nothing ever fanned an ephemeral key
    out — so there is provably no orphan to fear, while the three speculative
    deletes would be paid for on the way out of a charged action.
    """
    inner = _RecordingStorage()
    storage = VariantAwareObjectStorage(inner)
    key = f"{EPHEMERAL_OBJECT_KEY_PREFIXES[0]}u-1/{uuid4().hex}.png"
    await storage.put_bytes(
        object_key=key, content=_png_bytes(), content_type="image/png",
    )
    inner.delete_keys.clear()

    await storage.delete(object_key=key)

    assert inner.delete_keys == [key]


# ---------------------------------------------------------------------------
# put_bytes — the red line: variants never affect the original
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_encoder_failure_never_reaches_the_caller(
    monkeypatch: pytest.MonkeyPatch,
    caplog,  # noqa: ANN001
) -> None:
    def boom(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("libwebp exploded")

    monkeypatch.setattr(
        "kokoro_link.infrastructure.storage.variant_aware.encode_variants",
        boom,
    )
    inner = InMemoryObjectStorage()
    storage = VariantAwareObjectStorage(inner)
    payload = _png_bytes()

    with caplog.at_level(logging.DEBUG):
        stored = await storage.put_bytes(
            object_key=_ORIGINAL_KEY,
            content=payload,
            content_type="image/png",
        )

    assert stored.object_key == _ORIGINAL_KEY
    assert stored.size_bytes == len(payload)
    assert await inner.get_bytes(object_key=_ORIGINAL_KEY) == payload
    assert any("variant encoding failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_variant_write_failure_never_reaches_the_caller() -> None:
    class _RejectsVariantWrites(InMemoryObjectStorage):
        async def put_bytes(self, **kwargs):  # noqa: ANN003, ANN201
            if kwargs["object_key"].endswith(".webp"):
                raise ObjectStorageError("disk full")
            return await super().put_bytes(**kwargs)

    inner = _RejectsVariantWrites()
    storage = VariantAwareObjectStorage(inner)
    payload = _png_bytes()

    stored = await storage.put_bytes(
        object_key=_ORIGINAL_KEY,
        content=payload,
        content_type="image/png",
    )

    assert stored.object_key == _ORIGINAL_KEY
    assert await inner.get_bytes(object_key=_ORIGINAL_KEY) == payload
    for variant_key in _variant_keys(_ORIGINAL_KEY):
        assert await inner.stat(object_key=variant_key) is None


@pytest.mark.asyncio
async def test_original_write_failure_still_propagates() -> None:
    """Fail-soft covers variants only — a failed *original* is still a
    failed write, and the caller must hear about it."""

    class _RejectsEverything(InMemoryObjectStorage):
        async def put_bytes(self, **kwargs):  # noqa: ANN003, ANN201
            raise ObjectStorageError("disk full")

    storage = VariantAwareObjectStorage(_RejectsEverything())

    with pytest.raises(ObjectStorageError):
        await storage.put_bytes(
            object_key=_ORIGINAL_KEY,
            content=_png_bytes(),
            content_type="image/png",
        )


@pytest.mark.asyncio
async def test_partial_variant_failure_keeps_the_variants_that_worked() -> None:
    class _RejectsOneTier(InMemoryObjectStorage):
        async def put_bytes(self, **kwargs):  # noqa: ANN003, ANN201
            if kwargs["object_key"].endswith(".w768.webp"):
                raise ObjectStorageError("nope")
            return await super().put_bytes(**kwargs)

    inner = _RejectsOneTier()
    storage = VariantAwareObjectStorage(inner)

    await storage.put_bytes(
        object_key=_ORIGINAL_KEY,
        content=_png_bytes(),
        content_type="image/png",
    )

    assert await inner.stat(
        object_key=derive_variant_key(_ORIGINAL_KEY, "w320"),
    ) is not None
    assert await inner.stat(
        object_key=derive_variant_key(_ORIGINAL_KEY, "w768"),
    ) is None
    assert await inner.stat(
        object_key=derive_variant_key(_ORIGINAL_KEY, "full"),
    ) is not None


@pytest.mark.asyncio
async def test_undecodable_image_bytes_store_the_original_alone() -> None:
    """A declared-but-broken image: original stored, zero variants, no raise."""
    inner = InMemoryObjectStorage()
    storage = VariantAwareObjectStorage(inner)

    stored = await storage.put_bytes(
        object_key=_ORIGINAL_KEY,
        content=b"not really a png",
        content_type="image/png",
    )

    assert stored.size_bytes == len(b"not really a png")
    for variant_key in _variant_keys(_ORIGINAL_KEY):
        assert await inner.stat(object_key=variant_key) is None


# ---------------------------------------------------------------------------
# copy — album candidate commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copy_brings_the_variants_along() -> None:
    inner = InMemoryObjectStorage()
    storage = VariantAwareObjectStorage(inner)
    source = "characters/char-1/candidates/a.png"
    destination = "characters/char-1/stage/a.png"
    await storage.put_bytes(
        object_key=source,
        content=_png_bytes(),
        content_type="image/png",
    )

    copied = await storage.copy(source_key=source, destination_key=destination)

    assert copied.object_key == destination
    for name in (spec.name for spec in VARIANT_SPECS):
        source_meta = await inner.stat(object_key=derive_variant_key(source, name))
        dest_meta = await inner.stat(
            object_key=derive_variant_key(destination, name),
        )
        assert source_meta is not None and dest_meta is not None, name
        assert dest_meta.sha256 == source_meta.sha256, name


@pytest.mark.asyncio
async def test_copy_tolerates_an_original_that_has_no_variants(
    caplog,  # noqa: ANN001
) -> None:
    """Legacy rows written before IV1 — the copy must still succeed, and
    must not shout about it (every pre-IV1 image hits this path)."""
    inner = InMemoryObjectStorage()
    storage = VariantAwareObjectStorage(inner)
    source = "characters/char-1/candidates/legacy.png"
    destination = "characters/char-1/stage/legacy.png"
    # Written straight to the adapter: the decorator never saw it, so it
    # has an original and nothing else, exactly like existing storage.
    await inner.put_bytes(
        object_key=source,
        content=_png_bytes(),
        content_type="image/png",
    )

    with caplog.at_level(logging.DEBUG):
        copied = await storage.copy(
            source_key=source, destination_key=destination,
        )

    assert copied.object_key == destination
    assert await inner.get_bytes(object_key=destination) == await inner.get_bytes(
        object_key=source,
    )
    for variant_key in _variant_keys(destination):
        assert await inner.stat(object_key=variant_key) is None
    noisy = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert noisy == [], [r.getMessage() for r in noisy]


@pytest.mark.asyncio
async def test_copy_tolerates_a_partially_backfilled_original() -> None:
    """Only some tiers exist (narrow source, or an interrupted backfill)."""
    inner = InMemoryObjectStorage()
    storage = VariantAwareObjectStorage(inner)
    source = "characters/char-1/candidates/a.png"
    destination = "characters/char-1/stage/a.png"
    await inner.put_bytes(
        object_key=source, content=_png_bytes(), content_type="image/png",
    )
    await inner.put_bytes(
        object_key=derive_variant_key(source, "full"),
        content=b"WEBP-FULL",
        content_type="image/webp",
    )

    await storage.copy(source_key=source, destination_key=destination)

    assert await inner.get_bytes(
        object_key=derive_variant_key(destination, "full"),
    ) == b"WEBP-FULL"
    assert await inner.stat(
        object_key=derive_variant_key(destination, "w320"),
    ) is None


@pytest.mark.asyncio
async def test_copy_variant_failure_never_reaches_the_caller() -> None:
    class _RejectsVariantCopies(InMemoryObjectStorage):
        async def copy(self, **kwargs):  # noqa: ANN003, ANN201
            if kwargs["source_key"].endswith(".webp"):
                raise ObjectStorageError("backend said no")
            return await super().copy(**kwargs)

    inner = _RejectsVariantCopies()
    storage = VariantAwareObjectStorage(inner)
    source = "characters/char-1/candidates/a.png"
    destination = "characters/char-1/stage/a.png"
    await storage.put_bytes(
        object_key=source, content=_png_bytes(), content_type="image/png",
    )

    copied = await storage.copy(source_key=source, destination_key=destination)

    assert copied.object_key == destination
    assert await inner.stat(object_key=destination) is not None


@pytest.mark.asyncio
async def test_copy_of_non_image_does_not_probe_variants() -> None:
    inner = _RecordingStorage()
    storage = VariantAwareObjectStorage(inner)
    await inner.put_bytes(
        object_key="tts/char-1/a.wav", content=b"WAV", content_type="audio/wav",
    )

    await storage.copy(
        source_key="tts/char-1/a.wav", destination_key="tts/char-1/b.wav",
    )

    assert inner.copy_pairs == [("tts/char-1/a.wav", "tts/char-1/b.wav")]


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_the_variants_too() -> None:
    inner = InMemoryObjectStorage()
    storage = VariantAwareObjectStorage(inner)
    await storage.put_bytes(
        object_key=_ORIGINAL_KEY,
        content=_png_bytes(),
        content_type="image/png",
    )

    await storage.delete(object_key=_ORIGINAL_KEY)

    assert await inner.stat(object_key=_ORIGINAL_KEY) is None
    for variant_key in _variant_keys(_ORIGINAL_KEY):
        assert await inner.stat(object_key=variant_key) is None, variant_key


@pytest.mark.asyncio
async def test_delete_is_unconditional_because_orphans_stay_servable() -> None:
    """No content-type gate on delete: the route resolves ``?v=`` before
    the original, so a surviving variant would keep serving a deleted
    image. Deleting a key that never had variants is a harmless no-op."""
    inner = _RecordingStorage()
    storage = VariantAwareObjectStorage(inner)
    await inner.put_bytes(
        object_key="tts/char-1/a.wav", content=b"WAV", content_type="audio/wav",
    )

    await storage.delete(object_key="tts/char-1/a.wav")

    assert inner.delete_keys == [
        "tts/char-1/a.wav",
        *_variant_keys("tts/char-1/a.wav"),
    ]


@pytest.mark.asyncio
async def test_delete_of_a_variant_key_does_not_cascade() -> None:
    inner = _RecordingStorage()
    storage = VariantAwareObjectStorage(inner)
    variant_key = derive_variant_key(_ORIGINAL_KEY, "w320")

    await storage.delete(object_key=variant_key)

    assert inner.delete_keys == [variant_key]


@pytest.mark.asyncio
async def test_delete_variant_failure_never_reaches_the_caller() -> None:
    class _RejectsVariantDeletes(InMemoryObjectStorage):
        async def delete(self, *, object_key: str) -> None:
            if object_key.endswith(".webp"):
                raise ObjectStorageError("backend said no")
            await super().delete(object_key=object_key)

    inner = _RejectsVariantDeletes()
    storage = VariantAwareObjectStorage(inner)
    await inner.put_bytes(
        object_key=_ORIGINAL_KEY,
        content=_png_bytes(),
        content_type="image/png",
    )

    await storage.delete(object_key=_ORIGINAL_KEY)

    assert await inner.stat(object_key=_ORIGINAL_KEY) is None


@pytest.mark.asyncio
async def test_original_delete_failure_still_propagates() -> None:
    class _RejectsEverything(InMemoryObjectStorage):
        async def delete(self, *, object_key: str) -> None:
            raise ObjectStorageError("backend said no")

    storage = VariantAwareObjectStorage(_RejectsEverything())

    with pytest.raises(ObjectStorageError):
        await storage.delete(object_key=_ORIGINAL_KEY)


# ---------------------------------------------------------------------------
# Plain delegation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_side_methods_delegate_unchanged() -> None:
    inner = InMemoryObjectStorage()
    storage = VariantAwareObjectStorage(inner)
    await storage.put_bytes(
        object_key=_ORIGINAL_KEY, content=b"PNG", content_type="image/png",
    )

    assert await storage.get_bytes(object_key=_ORIGINAL_KEY) == b"PNG"
    meta = await storage.stat(object_key=_ORIGINAL_KEY)
    assert meta is not None and meta.object_key == _ORIGINAL_KEY
    url = await storage.public_url(object_key=_ORIGINAL_KEY)
    assert url == await inner.public_url(object_key=_ORIGINAL_KEY)
    assert storage.object_key_from_url(url) == _ORIGINAL_KEY


# ---------------------------------------------------------------------------
# Optional capability forwarding (``iter_bytes``)
#
# ``SupportsObjectStream`` is detected with ``getattr``, so a decorator
# that drops it degrades the public media route back to buffering whole
# objects in this process — silently, with nothing red anywhere. These
# pin both directions of the forwarding.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_bytes_survives_the_decorator() -> None:
    inner = InMemoryObjectStorage()
    storage = VariantAwareObjectStorage(inner)
    payload = bytes(range(256))
    await storage.put_bytes(
        object_key=_ORIGINAL_KEY, content=payload, content_type="image/png",
    )

    assert callable(getattr(storage, "iter_bytes", None))
    chunks = [
        chunk
        async for chunk in storage.iter_bytes(
            object_key=_ORIGINAL_KEY, chunk_size=100,
        )
    ]

    assert b"".join(chunks) == payload
    assert [len(chunk) for chunk in chunks] == [100, 100, 56]


def test_absent_capability_is_forwarded_as_absent() -> None:
    """Wrapping an adapter that cannot stream must not claim it can.

    An unconditional ``iter_bytes`` on the decorator would pass the
    route's ``getattr`` probe and then blow up mid-body, after the
    headers were already sent.
    """

    class _CorePortOnly:
        async def stat(self, *, object_key):  # noqa: ANN001, ANN202
            return None

        async def get_bytes(self, *, object_key):  # noqa: ANN001, ANN202
            return b""

    storage = VariantAwareObjectStorage(_CorePortOnly())  # type: ignore[arg-type]

    assert getattr(storage, "iter_bytes", None) is None
    with pytest.raises(AttributeError):
        storage.iter_bytes  # noqa: B018


@pytest.mark.asyncio
async def test_purge_prefix_survives_the_decorator() -> None:
    """CD1 ``SupportsPrefixPurge``: forwarded like ``iter_bytes`` — the
    decorator declares no ``purge_prefix`` of its own, so ``__getattr__``
    is the only thing standing between a caller and the inner adapter's
    capability."""
    inner = InMemoryObjectStorage()
    storage = VariantAwareObjectStorage(inner)
    await storage.put_bytes(
        object_key=_ORIGINAL_KEY, content=_png_bytes(), content_type="image/png",
    )

    assert callable(getattr(storage, "purge_prefix", None))
    deleted = await storage.purge_prefix(prefix="characters/char-1/")

    assert deleted > 0
    assert await inner.stat(object_key=_ORIGINAL_KEY) is None
    for variant_key in _variant_keys(_ORIGINAL_KEY):
        assert await inner.stat(object_key=variant_key) is None


def test_purge_prefix_absent_on_inner_is_forwarded_as_absent() -> None:
    """The counterpart to the write-side pin above: wrapping an adapter
    that cannot purge a prefix must not appear to gain the capability —
    an unconditional ``purge_prefix`` here would pass a caller's
    ``getattr`` probe and then blow up (or silently no-op) for adapters
    that never implemented it."""

    class _CorePortOnly:
        async def stat(self, *, object_key):  # noqa: ANN001, ANN202
            return None

        async def get_bytes(self, *, object_key):  # noqa: ANN001, ANN202
            return b""

    storage = VariantAwareObjectStorage(_CorePortOnly())  # type: ignore[arg-type]

    assert getattr(storage, "purge_prefix", None) is None
    with pytest.raises(AttributeError):
        storage.purge_prefix  # noqa: B018


def test_private_attributes_are_not_forwarded() -> None:
    """Keeps the wrapped adapter's internals encapsulated and keeps
    ``__getattr__`` from recursing on a half-built instance."""
    storage = VariantAwareObjectStorage(InMemoryObjectStorage())

    with pytest.raises(AttributeError):
        storage._public_base_url  # noqa: B018


def test_wrapped_storage_still_streams_through_the_public_route() -> None:
    """End-to-end: the route must reach ``iter_bytes`` past the decorator.

    This is the assertion that would have caught IV1 silently undoing
    IV2 — everything else about the response looks identical when the
    buffered fallback is taken.
    """
    inner = _RecordingStorage()
    storage = VariantAwareObjectStorage(inner)
    payload = b"X" * 4096
    app = FastAPI()
    app.state.container = SimpleNamespace(object_storage=storage)
    app.include_router(router)
    client = TestClient(app)

    asyncio.run(
        storage.put_bytes(
            object_key=_ORIGINAL_KEY,
            content=payload,
            content_type="image/png",
        ),
    )
    inner.get_bytes_keys.clear()
    inner.iter_bytes_keys.clear()

    response = client.get(f"/v1/public/{_ORIGINAL_KEY}")

    assert response.status_code == 200
    assert response.content == payload
    assert inner.iter_bytes_keys == [_ORIGINAL_KEY]
    assert inner.get_bytes_keys == []


def test_wrapped_storage_serves_the_variant_it_generated() -> None:
    """The write side and the read side must agree on the derived key —
    the one place where IV0/IV1/IV2 could drift apart without any test
    failing on either side alone."""
    storage = VariantAwareObjectStorage(InMemoryObjectStorage())
    app = FastAPI()
    app.state.container = SimpleNamespace(object_storage=storage)
    app.include_router(router)
    client = TestClient(app)

    asyncio.run(
        storage.put_bytes(
            object_key=_ORIGINAL_KEY,
            content=_png_bytes(),
            content_type="image/png",
        ),
    )

    original = client.get(f"/v1/public/{_ORIGINAL_KEY}")
    thumb = client.get(f"/v1/public/{_ORIGINAL_KEY}?v=w320")

    assert original.status_code == thumb.status_code == 200
    assert thumb.headers["x-object-key"] == derive_variant_key(
        _ORIGINAL_KEY, "w320",
    )
    assert thumb.headers["content-type"].startswith("image/webp")
    assert len(thumb.content) < len(original.content)


# ---------------------------------------------------------------------------
# Container wiring (D3: one place, no call-site changes)
# ---------------------------------------------------------------------------


def test_container_wires_the_variant_decorator_around_the_adapter() -> None:
    from kokoro_link.bootstrap.container import _build_object_storage
    from kokoro_link.bootstrap.settings import AppSettings, ObjectStorageSettings

    settings = AppSettings(
        database_url="",
        storage=ObjectStorageSettings(provider="memory"),
    )

    storage = _build_object_storage(settings)

    assert isinstance(storage, VariantAwareObjectStorage)
    assert callable(getattr(storage, "iter_bytes", None))
