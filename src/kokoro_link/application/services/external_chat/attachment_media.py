"""Pure media validation for external-chat attachments (LH2).

No I/O, no framework: given raw bytes and a client-declared content type,
decide whether they are a supported raster image and, when the header can be
parsed cheaply, what the pixel dimensions are. Kept separate from the
service so the sniffing/parsing rules stay unit-testable in isolation and the
service reads as orchestration only.

Two independent gates the service composes:

* :func:`sniff_image` — the declared type must be one of the four supported
  raster types *and* the leading magic bytes must actually match it. A JPEG
  masquerading as ``image/png`` (or an arbitrary blob) yields ``None``.
* :func:`image_dimensions` — a best-effort, dependency-free header decode of
  width/height. Pillow is deliberately not a dependency, so this reads only
  the handful of header bytes each format needs. ``None`` means "could not
  determine" — the caller treats that as "no dimension evidence", never as a
  rejection.
"""

from __future__ import annotations

from dataclasses import dataclass

# Declared content type -> canonical file extension. ``image/jpg`` is
# intentionally absent: only the four contract-supported types are accepted.
_SUPPORTED: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
}

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
_JPEG_MAGIC = b"\xff\xd8\xff"
_GIF_MAGICS = (b"GIF87a", b"GIF89a")


@dataclass(frozen=True, slots=True)
class SniffedImage:
    """A declared type that both is supported and matches its magic bytes."""

    media_type: str
    extension: str


def sniff_image(*, declared_content_type: str, data: bytes) -> SniffedImage | None:
    """Return the sniffed image when the declared type is supported and the
    magic bytes agree, else ``None`` (unsupported type or content mismatch)."""
    declared = (declared_content_type or "").split(";")[0].strip().lower()
    extension = _SUPPORTED.get(declared)
    if extension is None:
        return None
    if not _magic_matches(declared, data):
        return None
    return SniffedImage(media_type=declared, extension=extension)


def _magic_matches(media_type: str, data: bytes) -> bool:
    if media_type == "image/png":
        return data.startswith(_PNG_MAGIC)
    if media_type == "image/jpeg":
        return data.startswith(_JPEG_MAGIC)
    if media_type == "image/gif":
        return any(data.startswith(magic) for magic in _GIF_MAGICS)
    if media_type == "image/webp":
        return (
            len(data) >= 12
            and data[0:4] == b"RIFF"
            and data[8:12] == b"WEBP"
        )
    return False


def image_dimensions(*, media_type: str, data: bytes) -> tuple[int, int] | None:
    """Best-effort ``(width, height)`` from the raster header, or ``None``.

    Dependency-free: reads only the header fields each format exposes. A
    ``None`` result means the dimensions could not be recovered from the
    header and carries no safety verdict on its own."""
    if media_type == "image/png":
        return _png_dimensions(data)
    if media_type == "image/gif":
        return _gif_dimensions(data)
    if media_type == "image/jpeg":
        return _jpeg_dimensions(data)
    if media_type == "image/webp":
        return _webp_dimensions(data)
    return None


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    # 8-byte signature, then the IHDR chunk whose payload opens with the
    # big-endian uint32 width and height at fixed offsets 16 and 20.
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width <= 0 or height <= 0:
        return None
    return width, height


def _gif_dimensions(data: bytes) -> tuple[int, int] | None:
    # Logical Screen Descriptor sits right after the 6-byte header; width and
    # height are little-endian uint16.
    if len(data) < 10:
        return None
    width = int.from_bytes(data[6:8], "little")
    height = int.from_bytes(data[8:10], "little")
    if width <= 0 or height <= 0:
        return None
    return width, height


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    # Walk the marker segments until a Start-Of-Frame (SOFn) carries the
    # sample dimensions. C4 (DHT), C8 (JPG), CC (DAC) are not frame headers.
    size = len(data)
    i = 2  # skip the SOI (FFD8) marker
    while i + 9 <= size:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[i + 5:i + 7], "big")
            width = int.from_bytes(data[i + 7:i + 9], "big")
            if width <= 0 or height <= 0:
                return None
            return width, height
        segment_length = int.from_bytes(data[i + 2:i + 4], "big")
        if segment_length < 2:
            return None
        i += 2 + segment_length
    return None


def _webp_dimensions(data: bytes) -> tuple[int, int] | None:
    # RIFF container: the fourcc at offset 12 selects the codec chunk layout.
    if len(data) < 16:
        return None
    fourcc = data[12:16]
    if fourcc == b"VP8X":
        return _webp_vp8x_dimensions(data)
    if fourcc == b"VP8L":
        return _webp_vp8l_dimensions(data)
    if fourcc == b"VP8 ":
        return _webp_vp8_dimensions(data)
    return None


def _webp_vp8x_dimensions(data: bytes) -> tuple[int, int] | None:
    # Extended format: canvas width-1/height-1 as 24-bit little-endian at
    # offsets 24 and 27.
    if len(data) < 30:
        return None
    width = int.from_bytes(data[24:27], "little") + 1
    height = int.from_bytes(data[27:30], "little") + 1
    return width, height


def _webp_vp8_dimensions(data: bytes) -> tuple[int, int] | None:
    # Lossy: after the 20-byte RIFF+chunk+frame-tag preamble, the start code
    # 9d012a precedes two 14-bit little-endian dimensions.
    if len(data) < 30 or data[23:26] != b"\x9d\x01\x2a":
        return None
    width = int.from_bytes(data[26:28], "little") & 0x3FFF
    height = int.from_bytes(data[28:30], "little") & 0x3FFF
    if width <= 0 or height <= 0:
        return None
    return width, height


def _webp_vp8l_dimensions(data: bytes) -> tuple[int, int] | None:
    # Lossless: 0x2f signature at offset 20, then 14 bits width-1 and 14 bits
    # height-1 packed little-endian across the following four bytes.
    if len(data) < 25 or data[20] != 0x2F:
        return None
    bits = int.from_bytes(data[21:25], "little")
    width = (bits & 0x3FFF) + 1
    height = ((bits >> 14) & 0x3FFF) + 1
    return width, height
