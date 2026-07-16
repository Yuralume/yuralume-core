"""Minimal image header probing for Telegram sendPhoto pre-flight.

Telegram's ``sendPhoto`` has two undocumented dimension constraints
that reject the upload with a generic 400:

- ``width + height <= 10000`` pixels
- longer side ≤ shorter side × 20 (aspect ratio guard)

ComfyUI portraits (832×1216, 1024×1024) are well within limits, but
operators who plug in bigger workflows or forward user-supplied images
can trip them. Without pre-flight we'd get an opaque
``PHOTO_INVALID_DIMENSIONS`` back from Telegram; with pre-flight we
can degrade to ``sendDocument`` (works for any size + aspect) and log
the reason so ops can see why.

We deliberately **don't** depend on Pillow — it's a 20 MB native lib
for reading two integers from a header. The parsers below cover PNG,
JPEG, WebP, GIF (the formats our /uploads pipeline accepts). Anything
else returns ``None`` and callers fall back to ``sendPhoto`` with the
previous best-effort behaviour.
"""

from __future__ import annotations

import logging
import struct
from typing import Literal

_LOGGER = logging.getLogger(__name__)

MAX_PHOTO_DIMENSION_SUM = 10_000
"""width + height ceiling for sendPhoto. Source: Telegram 400 responses."""
MAX_PHOTO_ASPECT_RATIO = 20
"""longer_side / shorter_side ceiling for sendPhoto."""

PhotoDecision = Literal["photo", "document"]


def probe_image_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return ``(width, height)`` for a PNG / JPEG / WebP / GIF blob.

    ``None`` means we couldn't parse it (unknown format or truncated
    bytes). Callers should treat that as "send as photo anyway" rather
    than refusing the send — we lose the pre-flight protection but keep
    behaviour identical to pre-probe days.
    """
    # Smallest we handle is a GIF header (10 bytes). Per-format parsers
    # re-check their own minimums for the dimension fields.
    if len(data) < 10:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return _probe_png(data)
    if data.startswith(b"\xff\xd8"):
        return _probe_jpeg(data)
    if data.startswith(b"RIFF") and len(data) >= 12 and data[8:12] == b"WEBP":
        return _probe_webp(data)
    if data.startswith((b"GIF87a", b"GIF89a")):
        return _probe_gif(data)
    return None


def decide_photo_vs_document(
    data: bytes,
    *,
    log_context: str = "",
) -> PhotoDecision:
    """Decide ``sendPhoto`` vs ``sendDocument`` based on dimensions.

    Any of these push to ``document``:
    - we can't parse the blob (conservative: avoid surprising 400)
    - ``width + height > 10000``
    - longer side > shorter side × 20

    Every degrade path logs ``WARNING`` with the reason + dimensions
    (or ``unknown`` when unparseable) so ops can trace why Alice got a
    photo-preview and Bob got a file attachment. ``log_context`` is
    added to the log line (usually ``chat_ref=... url=...``).
    """
    dims = probe_image_dimensions(data)
    if dims is None:
        _LOGGER.info(
            "Telegram image probe: unparseable header, defaulting to sendPhoto %s",
            log_context,
        )
        # Unparseable: we deliberately return "photo" not "document" so
        # behaviour matches pre-probe defaults. The info log gives ops
        # a breadcrumb if a future 400 turns up.
        return "photo"

    width, height = dims
    if width <= 0 or height <= 0:
        _LOGGER.warning(
            "Telegram image probe: non-positive dims %dx%d, sending as document %s",
            width, height, log_context,
        )
        return "document"

    dim_sum = width + height
    if dim_sum > MAX_PHOTO_DIMENSION_SUM:
        _LOGGER.warning(
            "Telegram image probe: %dx%d (sum=%d > %d), sending as document %s",
            width, height, dim_sum, MAX_PHOTO_DIMENSION_SUM, log_context,
        )
        return "document"

    longer, shorter = max(width, height), min(width, height)
    if longer > shorter * MAX_PHOTO_ASPECT_RATIO:
        _LOGGER.warning(
            "Telegram image probe: %dx%d aspect ratio %.1f > %d, sending as document %s",
            width, height, longer / shorter, MAX_PHOTO_ASPECT_RATIO, log_context,
        )
        return "document"

    return "photo"


# ---------- format-specific parsers (private) ----------


def _probe_png(data: bytes) -> tuple[int, int] | None:
    """PNG IHDR: 8 signature bytes, 4 length, 4 "IHDR", then w, h as BE uint32."""
    if len(data) < 24:
        return None
    # width at [16:20], height at [20:24]
    try:
        width, height = struct.unpack(">II", data[16:24])
    except struct.error:
        return None
    return width, height


def _probe_jpeg(data: bytes) -> tuple[int, int] | None:
    """JPEG: scan for SOF markers (0xFFC0..0xFFCF, minus 0xFFC4/C8/CC).

    The marker payload starts with 2 bytes length + 1 byte precision +
    2 bytes height + 2 bytes width (big-endian).
    """
    # Skip past the initial SOI (0xFFD8)
    offset = 2
    length = len(data)
    while offset < length - 9:
        if data[offset] != 0xFF:
            return None  # malformed
        # Skip any 0xFF padding bytes
        while offset < length and data[offset] == 0xFF:
            offset += 1
        if offset >= length:
            return None
        marker = data[offset]
        offset += 1
        # Standalone markers (no payload)
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        # Payload length (includes the 2 length bytes themselves)
        if offset + 2 > length:
            return None
        (payload_len,) = struct.unpack(">H", data[offset:offset + 2])
        # SOF markers carry dimensions
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            # offset+2 skips length, +1 skips precision byte
            if offset + 7 > length:
                return None
            height, width = struct.unpack(">HH", data[offset + 3:offset + 7])
            return width, height
        offset += payload_len
    return None


def _probe_webp(data: bytes) -> tuple[int, int] | None:
    """WebP: RIFF container with VP8 / VP8L / VP8X sub-chunks.

    The three sub-chunks encode dimensions slightly differently; this
    covers all three since ComfyUI / browser uploads can yield any.
    """
    if len(data) < 30:
        return None
    chunk_type = data[12:16]
    try:
        if chunk_type == b"VP8 ":
            # Lossy: width/height at offset 26 (14-bit little-endian)
            width, height = struct.unpack("<HH", data[26:30])
            return width & 0x3FFF, height & 0x3FFF
        if chunk_type == b"VP8L":
            # Lossless: packed at offset 21 (14-bit + 14-bit + flags)
            if len(data) < 25:
                return None
            b0, b1, b2, b3 = data[21:25]
            width = 1 + (((b1 & 0x3F) << 8) | b0)
            height = 1 + (((b3 & 0x0F) << 10) | (b2 << 2) | ((b1 & 0xC0) >> 6))
            return width, height
        if chunk_type == b"VP8X":
            # Extended: 24-bit LE canvas dims at offset 24, stored as (dim-1)
            if len(data) < 30:
                return None
            w_bytes = data[24:27] + b"\x00"
            h_bytes = data[27:30] + b"\x00"
            (width_minus_1,) = struct.unpack("<I", w_bytes)
            (height_minus_1,) = struct.unpack("<I", h_bytes)
            return width_minus_1 + 1, height_minus_1 + 1
    except struct.error:
        return None
    return None


def _probe_gif(data: bytes) -> tuple[int, int] | None:
    """GIF: width/height are LE uint16 at offsets 6 and 8."""
    if len(data) < 10:
        return None
    try:
        width, height = struct.unpack("<HH", data[6:10])
    except struct.error:
        return None
    return width, height
