"""Pure WebP variant encoder for object-storage images (IV0).

Given raw image bytes, :func:`encode_variants` returns zero or more resized
WebP variants — no storage I/O, no DB access, no framework dependency. It is
the single source of truth for the variant tier definitions
(:data:`VARIANT_SPECS`) and the object-key derivation rule
(:func:`derive_variant_key`) that every later consumer must share:

- IV1 (``VariantAwareObjectStorage`` storage-port decorator) calls
  :func:`encode_variants` after ``put_bytes`` and writes each result under
  :func:`derive_variant_key`'s output key.
- IV2 (the ``GET /v1/public/{object_key}?v=...`` route) derives the same
  keys to look up a variant, falling back to the original object on a miss.
- IV3 (the existing-image backfill script) must produce byte-identical keys
  to what IV1 would have produced at write time, or the route's ``stat()``
  lookup silently misses forever.

for the
product-level spec this module implements.

Deliberate Pillow dependency (D4)
----------------------------------
This repo has two places that explicitly avoid Pillow:
``application/services/external_chat/attachment_media.py`` and
``infrastructure/messaging/telegram/image_probe.py``. Both say so in their
own docstrings, and neither is touched by this module. Read those comments
before assuming "no Pillow" is a repo-wide rule — it is not.

Those two modules only need to *read a handful of header bytes* to recover
width/height; a few dozen lines of ``struct.unpack`` per format covers that
cheaply, so pulling in a ~20 MB native imaging library for two integers
would have been the wrong trade. **This module's job is different: it has
to re-encode pixels into WebP**, and there is no equivalent pure-Python
path for that — WebP is a real codec (block prediction, transforms,
entropy coding), not a header format. Pillow (with its bundled libwebp) is
the only supported dependency for a job like that, so IV0 accepts it. This
paragraph is the "someone already decided this, don't re-litigate it"
marker for the next agent who greps for "Pillow" and finds those two other
comments.
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image

__all__ = [
    "EncodedVariant",
    "ImageVariantSpec",
    "VARIANT_SPECS",
    "derive_variant_key",
    "encode_variants",
]


@dataclass(frozen=True, slots=True)
class ImageVariantSpec:
    """Declarative definition of one WebP variant tier.

    ``target_width`` is ``None`` for the ``full`` tier: original pixel
    dimensions, never resized. For the other tiers it is the target width
    in pixels; height is derived preserving aspect ratio.
    """

    name: str
    target_width: int | None
    quality: int


VARIANT_SPECS: tuple[ImageVariantSpec, ...] = (
    ImageVariantSpec(name="w320", target_width=320, quality=75),
    ImageVariantSpec(name="w768", target_width=768, quality=82),
    ImageVariantSpec(name="full", target_width=None, quality=85),
)
"""Canonical tier list per plan D1. Import this — do not restate the
(width, quality) numbers elsewhere; IV1 and IV3 both depend on there being
exactly one definition."""

_VARIANT_NAMES: frozenset[str] = frozenset(spec.name for spec in VARIANT_SPECS)

_VARIANT_KEY_SUFFIX = ".{name}.webp"


@dataclass(frozen=True, slots=True)
class EncodedVariant:
    """One successfully encoded WebP variant, keyed by tier name."""

    name: str
    content: bytes
    content_type: str
    width: int
    height: int


def derive_variant_key(object_key: str, variant_name: str) -> str:
    """Return the sibling storage key for ``variant_name`` of ``object_key``.

    Pure string derivation — no existence check, no I/O. The result is
    always ``{object_key}.{variant_name}.webp``, e.g.
    ``characters/abc/portrait.png`` + ``"w320"`` ->
    ``characters/abc/portrait.png.w320.webp``.

    This suffix form (rather than replacing the original extension) keeps
    the derivation a plain string concatenation — no need to know or strip
    the source format — and, per plan D1, keeps every produced segment
    within the object-key character allowlist enforced by
    :mod:`kokoro_link.infrastructure.storage.keys`
    (``[A-Za-z0-9._-]`` — variant names and ``.webp`` are already within
    that set, so no extra escaping is required here).

    Raises ``ValueError`` for an empty ``object_key`` or an unknown
    ``variant_name`` — both are programmer errors (callers should only ever
    pass names drawn from :data:`VARIANT_SPECS`), not runtime data
    conditions, so this function does *not* fold them into the "return
    empty" contract that :func:`encode_variants` uses for untrusted bytes.
    """
    if not object_key:
        raise ValueError("object_key must be non-empty")
    if variant_name not in _VARIANT_NAMES:
        raise ValueError(f"unknown variant name: {variant_name!r}")
    return object_key + _VARIANT_KEY_SUFFIX.format(name=variant_name)


def encode_variants(
    data: bytes,
    *,
    specs: tuple[ImageVariantSpec, ...] = VARIANT_SPECS,
) -> dict[str, EncodedVariant]:
    """Encode ``data`` into zero or more WebP variants, keyed by tier name.

    Contract (plan D1 / IV0 acceptance criteria):

    - Never raises. Empty bytes, non-image bytes, and truncated/corrupt
      images all yield ``{}`` rather than propagating a decode error —
      callers (the storage decorator, the backfill script) treat "no
      variants produced" uniformly regardless of *why*.
    - Never upscales. A tier is skipped entirely when the source width is
      already <= that tier's target width; there is no "upscale then
      pretend" fallback.
    - Alpha is preserved: sources with transparency (RGBA/LA, or palette
      images with a transparency entry) are encoded as RGBA WebP; opaque
      sources are encoded as RGB.
    - Pure function: no filesystem, network, or storage-port access.
    """
    if not data:
        return {}
    try:
        with Image.open(io.BytesIO(data)) as source:
            # Image.open() only parses the header lazily; force a full
            # decode now so truncated/corrupt pixel data raises here and
            # is caught below, instead of surfacing later inside .save().
            source.load()
            source_width, source_height = source.size
            if source_width <= 0 or source_height <= 0:
                return {}
            prepared = _prepare_for_encode(source)
            variants: dict[str, EncodedVariant] = {}
            for spec in specs:
                if spec.target_width is not None and source_width <= spec.target_width:
                    continue  # never upscale
                variants[spec.name] = _encode_one(
                    prepared,
                    spec,
                    source_width=source_width,
                    source_height=source_height,
                )
            return variants
    except Exception:
        # Pillow's failure surface for "this is not a decodable image" is
        # broad and codec-dependent: UnidentifiedImageError (unrecognized
        # format), OSError (truncated file), DecompressionBombError
        # (pathological declared dimensions), plus assorted ValueError /
        # SyntaxError raised by individual format plugins. Enumerating them
        # would just be restating "anything Pillow can throw while decoding
        # untrusted bytes" — the acceptance contract here is "never raise",
        # so every one of those collapses to the same "no variants" result.
        return {}


def _prepare_for_encode(image: Image.Image) -> Image.Image:
    """Normalize to a mode WebP encodes cleanly: RGBA when the source
    carries transparency, RGB otherwise."""
    has_alpha = image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    )
    return image.convert("RGBA") if has_alpha else image.convert("RGB")


def _encode_one(
    image: Image.Image,
    spec: ImageVariantSpec,
    *,
    source_width: int,
    source_height: int,
) -> EncodedVariant:
    if spec.target_width is None:
        resized = image
        width, height = source_width, source_height
    else:
        width = spec.target_width
        height = max(1, round(source_height * spec.target_width / source_width))
        resized = image.resize((width, height), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    resized.save(buffer, format="WEBP", quality=spec.quality)
    return EncodedVariant(
        name=spec.name,
        content=buffer.getvalue(),
        content_type="image/webp",
        width=width,
        height=height,
    )
