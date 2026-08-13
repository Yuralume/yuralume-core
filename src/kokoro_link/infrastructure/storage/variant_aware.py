"""Write-time WebP variant fan-out for the object-storage port (IV1).

``VariantAwareObjectStorage`` decorates any :class:`ObjectStoragePort` so
that every image that enters storage leaves behind three sibling WebP
renditions (see ``image_variants.VARIANT_SPECS``), every image that leaves
storage takes them with it, and every image that is copied brings them
along.

Why a decorator on the port rather than a call at each producer (plan D3):
``ObjectStoragePort`` is the single door for *all* image bytes — album
gacha, single-image generation, in-chat image tools, user uploads, feed
media, branching drama, inbound external-channel attachments — and the
single door for all deletions. One seam catches every one of them, so no
producer added later can silently skip variant generation.

**Red line (plan D3): a variant failure must never affect the original.**
The original write/copy is issued first and its result is returned
unchanged; everything variant-related after that is fail-soft, logged and
dropped. The read side (``GET /v1/public/{key}?v=`` — IV2) falls back to
the original whenever a variant is missing, so "no variants" is a fully
supported state, not a broken one. That fallback is also why this module
never retries and never queues: a missed variant costs bandwidth, not
correctness.

Ships D1/D3 of the image-delivery work.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from typing import Any

from kokoro_link.contracts.object_storage import (
    EPHEMERAL_OBJECT_KEY_PREFIXES,
    ObjectMetadata,
    ObjectNotFoundError,
    ObjectStoragePort,
    StoredObject,
)
from kokoro_link.infrastructure.storage.image_variants import (
    VARIANT_SPECS,
    derive_variant_key,
    encode_variants,
)

__all__ = ["VariantAwareObjectStorage"]

_LOGGER = logging.getLogger(__name__)

# The suffix set is *derived* from the same function that writes the keys,
# never restated. ``derive_variant_key`` is the one place allowed to know
# the key shape (IV0 docstring); recomputing ".w320.webp" here would be a
# second definition that can drift from the encoder and the route.
_SUFFIX_PROBE = "k"
_DERIVED_KEY_SUFFIXES: tuple[str, ...] = tuple(
    derive_variant_key(_SUFFIX_PROBE, spec.name)[len(_SUFFIX_PROBE):]
    for spec in VARIANT_SPECS
)


def _is_image_content_type(content_type: str | None) -> bool:
    """True for ``image/*``, tolerating parameters (``image/png; x=y``)."""
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower().startswith("image/")


def _is_ephemeral_key(object_key: str) -> bool:
    """True for staging scratch space that must not be fanned out.

    Objects under
    :data:`~kokoro_link.contracts.object_storage.EPHEMERAL_OBJECT_KEY_PREFIXES`
    (today: a character draft's reference image) exist only long enough for one
    upstream provider to fetch them, and no UI ever renders them — so every
    rendition is dead weight. Worse, it is dead weight billed to the player's
    wait: this decorator encodes and writes synchronously, so three WebP
    encodes plus three uploads land squarely in the foreground latency of a
    charged action, followed by three more deletes when the draft finishes.
    """
    return object_key.startswith(EPHEMERAL_OBJECT_KEY_PREFIXES)


def _is_derived_variant_key(object_key: str) -> bool:
    """True when ``object_key`` is itself one of our derived renditions.

    Guards against a second-order fan-out (``a.png.full.webp.w320.webp``)
    if a caller — most plausibly the IV3 backfill script, which writes
    variant keys directly — routes a variant back through this decorator.
    """
    return any(object_key.endswith(suffix) for suffix in _DERIVED_KEY_SUFFIXES)


class VariantAwareObjectStorage:
    """An :class:`ObjectStoragePort` that keeps WebP variants in lockstep.

    Non-image content types (TTS audio, JSON side-cars, exports) are pure
    pass-through: no encode attempt, no extra round trips.

    Optional capabilities (``iter_bytes`` and anything added later)
    ---------------------------------------------------------------
    :class:`~kokoro_link.contracts.object_storage.SupportsObjectStream`
    is deliberately *not* part of ``ObjectStoragePort``; callers detect it
    with ``getattr``. A decorator that does not forward it therefore does
    not fail loudly — it makes the public media proxy fall back to
    buffering whole objects in this process, which is exactly the cost IV2
    removed, with no error anywhere to show for it.

    :meth:`__getattr__` forwards such attributes to the wrapped adapter,
    and forwards **absence** just as faithfully: wrapping a core-port-only
    adapter still raises ``AttributeError`` for ``iter_bytes``, so
    capability detection keeps telling the truth. Declaring an explicit
    ``iter_bytes`` here would break that half — it would advertise
    streaming for adapters that cannot stream, and the resulting
    ``AttributeError`` would land *after* the response headers were
    already on the wire.
    """

    def __init__(self, inner: ObjectStoragePort) -> None:
        self._inner = inner

    # -- port methods that fan out ------------------------------------

    async def put_bytes(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        """Store the original, then best-effort store its variants.

        The original is written and its :class:`StoredObject` captured
        *before* a single pixel is decoded, so no encoder outcome can
        change what the caller gets back.

        Ephemeral keys are pure pass-through — see :func:`_is_ephemeral_key`.
        """
        if _is_ephemeral_key(object_key):
            return await self._inner.put_bytes(
                object_key=object_key,
                content=content,
                content_type=content_type,
                metadata=metadata,
            )
        stored = await self._inner.put_bytes(
            object_key=object_key,
            content=content,
            content_type=content_type,
            metadata=metadata,
        )
        if _is_image_content_type(content_type) and not _is_derived_variant_key(
            stored.object_key,
        ):
            await self._write_variants(
                object_key=stored.object_key,
                content=content,
                metadata=metadata,
            )
        return stored

    async def copy(
        self,
        *,
        source_key: str,
        destination_key: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        """Copy the original, then best-effort copy its variants.

        Copies rather than re-encodes: the source bytes are already in the
        backend, and pulling a multi-MB original back into this process to
        redo work that was done at write time would make album commits
        markedly slower for no gain. Sources that have no variants (rows
        that predate IV1, or a tier the encoder skipped because the source
        was already narrower than that tier) simply produce a destination
        without them — the read side falls back, and IV3's backfill is the
        component whose job is filling those in.
        """
        stored = await self._inner.copy(
            source_key=source_key,
            destination_key=destination_key,
            metadata=metadata,
        )
        if _is_image_content_type(stored.content_type) and not _is_derived_variant_key(
            source_key,
        ):
            await self._copy_variants(
                source_key=source_key,
                destination_key=stored.object_key,
                metadata=metadata,
            )
        return stored

    async def delete(self, *, object_key: str) -> None:
        """Delete the original, then best-effort delete its variants.

        Unlike write and copy this does **not** gate on content type, and
        that asymmetry is deliberate. The content type is not knowable
        here without an extra probe, and the probe answers ``None`` for
        exactly the case that matters — a retried delete after the
        original is already gone. A leftover variant is not inert: the
        route resolves ``?v=`` *before* the original, so an orphan would
        keep serving a picture whose original was deleted. Three no-op
        deletes on non-image keys is the price of that not happening.

        Ephemeral keys are the one exception, and it costs nothing: the write
        side never fanned them out, so there is provably no variant to orphan —
        whereas the three speculative deletes *would* be paid for, one round
        trip at a time, on the way out of a charged action.
        """
        await self._inner.delete(object_key=object_key)
        if _is_derived_variant_key(object_key) or _is_ephemeral_key(object_key):
            return
        for spec in VARIANT_SPECS:
            variant_key = derive_variant_key(object_key, spec.name)
            try:
                await self._inner.delete(object_key=variant_key)
            except ObjectNotFoundError:
                continue
            except Exception:
                _LOGGER.warning(
                    "failed to delete image variant %s (original already "
                    "deleted; variant may be orphaned)",
                    variant_key,
                    exc_info=True,
                )

    # -- plain delegation ---------------------------------------------

    async def get_bytes(self, *, object_key: str) -> bytes:
        return await self._inner.get_bytes(object_key=object_key)

    async def stat(self, *, object_key: str) -> ObjectMetadata | None:
        return await self._inner.stat(object_key=object_key)

    async def public_url(self, *, object_key: str) -> str:
        return await self._inner.public_url(object_key=object_key)

    def object_key_from_url(self, url: str) -> str | None:
        return self._inner.object_key_from_url(url)

    def __getattr__(self, name: str) -> Any:
        """Forward optional capabilities — see the class docstring.

        Private names are never forwarded: it keeps the wrapped adapter's
        internals encapsulated, and it stops ``__getattr__`` from
        recursing through ``self._inner`` before ``__init__`` has run
        (copy/pickle protocols probe dunder attributes on half-built
        objects).
        """
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._inner, name)

    # -- internals -----------------------------------------------------

    async def _write_variants(
        self,
        *,
        object_key: str,
        content: bytes,
        metadata: Mapping[str, str] | None,
    ) -> None:
        try:
            # Pillow encoding is CPU-bound native work measured in
            # hundreds of ms for a 1024x1536 source. Inline in the
            # coroutine it would stall every other request on this event
            # loop for that whole time; off-thread it stays inline for
            # *this* caller (plan D3: synchronous, no job queue) without
            # taxing anyone else.
            variants = await asyncio.to_thread(encode_variants, content)
        except Exception:
            # ``encode_variants`` contractually never raises; this catches
            # what is left (thread-pool exhaustion, MemoryError) so the
            # red line holds even when the contract is broken later.
            _LOGGER.warning(
                "image variant encoding failed for %s; original is stored, "
                "reads will fall back to it",
                object_key,
                exc_info=True,
            )
            return
        for variant in variants.values():
            variant_key = derive_variant_key(object_key, variant.name)
            try:
                await self._inner.put_bytes(
                    object_key=variant_key,
                    content=variant.content,
                    content_type=variant.content_type,
                    metadata=metadata,
                )
            except Exception:
                _LOGGER.warning(
                    "failed to store image variant %s; original is stored, "
                    "reads will fall back to it",
                    variant_key,
                    exc_info=True,
                )

    async def _copy_variants(
        self,
        *,
        source_key: str,
        destination_key: str,
        metadata: Mapping[str, str] | None,
    ) -> None:
        for spec in VARIANT_SPECS:
            variant_source = derive_variant_key(source_key, spec.name)
            variant_destination = derive_variant_key(destination_key, spec.name)
            try:
                await self._inner.copy(
                    source_key=variant_source,
                    destination_key=variant_destination,
                    metadata=metadata,
                )
            except ObjectNotFoundError:
                # Normal for anything written before IV1 and for tiers the
                # encoder skipped rather than upscale. Debug level on
                # purpose: on a not-yet-backfilled deployment this is the
                # common path, and a warning here would drown the log.
                _LOGGER.debug(
                    "no %s variant to copy for %s", spec.name, source_key,
                )
            except Exception:
                _LOGGER.warning(
                    "failed to copy image variant %s -> %s; destination "
                    "reads will fall back to the original",
                    variant_source,
                    variant_destination,
                    exc_info=True,
                )
