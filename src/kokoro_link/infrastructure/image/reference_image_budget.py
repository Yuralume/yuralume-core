"""How much reference art may ride *inside* a generation request (FC1).

EC6 made the partner's reference art travel in the request body, because
the upstream model sits on the far side of the Gateway and cannot fetch a
URL only this deployment serves. That decision quietly turned the art's
file size into a property of the HTTP call: base64 inflates every byte by
4/3, and the Gateway rejects an oversized body outright. A single 6 MB card
scan therefore stops being "a big picture" and becomes a hard failure on
every generation surface for that character — portrait, gacha, chat, feed.

So the budget is enforced before the bytes are encoded, and the resolution
is the thing that gives way. A likeness anchor needs recognisable features,
not print resolution: shrinking the art keeps the lock, while refusing to
send it loses the lock, and failing the request loses the picture. Art that
still will not fit after shrinking is dropped — the same fail-soft EC6
already applies to art it cannot read.

Re-encoding is delegated to :mod:`kokoro_link.infrastructure.storage.image_variants`
rather than reimplemented: it already owns the "never raise on untrusted
bytes" and "preserve alpha" contracts this path needs, and one call with
the whole ladder decodes the source image exactly once. It does encode
every rung even when the first would have done — a bounded waste on a path
that only runs for art that was already too big, paid to keep a single
encoder in the codebase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from kokoro_link.infrastructure.storage.image_variants import (
    ImageVariantSpec,
    encode_variants,
)

__all__ = [
    "DEFAULT_INLINE_PAYLOAD_BUDGET_BYTES",
    "DEFAULT_MAX_REFERENCE_IMAGE_BYTES",
    "FittedImage",
    "fit_reference_image",
]

_LOGGER = logging.getLogger(__name__)

DEFAULT_MAX_REFERENCE_IMAGE_BYTES = 1_500_000
"""Per-image ceiling on the raw bytes that get base64'd into the request.

1.5 MB of source becomes ~2 MB of data URI, so several such inputs still
sit well inside the Gateway's inbound limit while leaving a typical card
scan untouched — the shrink path is for outliers, not for everything.
"""

DEFAULT_INLINE_PAYLOAD_BUDGET_BYTES = 6 * 1024 * 1024
"""Ceiling on the *encoded* image bytes of one request, all inputs summed.

Measured on the data URIs themselves because that is what the JSON body
actually carries. Per-image caps alone bound nothing when a request may
hold several images, and it is the body — not any one picture — that the
Gateway refuses.
"""

_LADDER_QUALITY = 80

_LADDER: tuple[ImageVariantSpec, ...] = (
    # ``target_width=None`` is re-encode-at-source-size: a losslessly stored
    # PNG often clears the budget on compression alone, with no pixels lost.
    ImageVariantSpec(name="fit-source", target_width=None, quality=_LADDER_QUALITY),
    ImageVariantSpec(name="fit-1280", target_width=1280, quality=_LADDER_QUALITY),
    ImageVariantSpec(name="fit-1024", target_width=1024, quality=_LADDER_QUALITY),
    ImageVariantSpec(name="fit-768", target_width=768, quality=_LADDER_QUALITY),
    ImageVariantSpec(name="fit-512", target_width=512, quality=_LADDER_QUALITY),
)


@dataclass(frozen=True, slots=True)
class FittedImage:
    """Image bytes known to sit within the budget, with their media type."""

    content: bytes
    media_type: str


def fit_reference_image(
    data: bytes,
    *,
    media_type: str,
    max_bytes: int,
) -> FittedImage | None:
    """``data`` reduced to at most ``max_bytes``, or ``None`` if impossible.

    Bytes already inside the budget are returned untouched — including
    their original media type — so the common case stays byte-identical to
    what EC6 sent. Otherwise the largest rung of the reduction ladder that
    fits wins: keeping the most pixels the budget allows is what preserves
    the likeness the art exists to lock.

    ``None`` means "cannot be inlined at all" (not a decodable image, or no
    rung small enough), which the caller treats exactly as it treats art it
    could not read: draw the picture without the lock.
    """
    if not data:
        return None
    if len(data) <= max_bytes:
        return FittedImage(content=data, media_type=media_type)
    variants = encode_variants(data, specs=_LADDER)
    fitting = [
        variant for variant in variants.values()
        if len(variant.content) <= max_bytes
    ]
    if not fitting:
        _LOGGER.warning(
            "reference image does not fit the request budget "
            "(%d bytes, cap %d) and could not be reduced; "
            "drawing without it",
            len(data), max_bytes,
        )
        return None
    best = max(fitting, key=lambda variant: variant.width)
    _LOGGER.info(
        "reference image reduced to fit the request budget: "
        "%d bytes -> %d bytes at %dpx wide",
        len(data), len(best.content), best.width,
    )
    return FittedImage(content=best.content, media_type=best.content_type)
