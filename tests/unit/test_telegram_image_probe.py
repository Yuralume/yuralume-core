"""BDD for Telegram image dimension probing.

Covers the pure-function side of the sendPhoto vs sendDocument
decision. Adapter-level integration (actual routing) lives in
``test_telegram_adapter_attachments.py``.
"""

from __future__ import annotations

import struct

import pytest

from kokoro_link.infrastructure.messaging.telegram.image_probe import (
    MAX_PHOTO_ASPECT_RATIO,
    MAX_PHOTO_DIMENSION_SUM,
    decide_photo_vs_document,
    probe_image_dimensions,
)


# ---------- probe: format-by-format sanity ----------


def _fake_png(width: int, height: int) -> bytes:
    """Minimum valid PNG signature + IHDR length/tag + dims."""
    sig = b"\x89PNG\r\n\x1a\n"
    ihdr_len = struct.pack(">I", 13)
    ihdr_tag = b"IHDR"
    dims = struct.pack(">II", width, height)
    return sig + ihdr_len + ihdr_tag + dims + b"\x08\x02\x00\x00\x00"


def _fake_jpeg(width: int, height: int) -> bytes:
    """SOI + SOF0 marker carrying the dimensions.

    Real JPEGs have many other segments; we just need the scanner to
    find the SOF0 block without tripping on truncated intermediaries.
    """
    soi = b"\xff\xd8"
    sof0_marker = b"\xff\xc0"
    # length (incl. length bytes) = 2 len + 1 precision + 2 h + 2 w + 1 Nc + 3 per comp × 3
    payload = struct.pack(">BHHB", 8, height, width, 3) + b"\x01\x22\x00\x02\x11\x01\x03\x11\x01"
    length = struct.pack(">H", 2 + len(payload))
    return soi + sof0_marker + length + payload + b"\xff\xd9"


def _fake_gif(width: int, height: int) -> bytes:
    """GIF89a header: 6-byte magic + LE uint16 width + LE uint16 height."""
    return b"GIF89a" + struct.pack("<HH", width, height) + b"\x00\x00\x00"


def _fake_webp_vp8(width: int, height: int) -> bytes:
    """Minimum RIFF/WEBP/VP8 container with width/height at the VP8 key
    frame bitstream offset (3 bytes frame tag + 3 bytes start code +
    2 bytes W + 2 bytes H, all little-endian)."""
    vp8_payload = (
        b"\x00\x00\x00"     # frame tag (3 bytes)
        + b"\x9d\x01\x2a"   # start code for keyframe
        + struct.pack("<HH", width, height)
        + b"\x00" * 4       # trailing padding
    )
    vp8_chunk = b"VP8 " + struct.pack("<I", len(vp8_payload)) + vp8_payload
    total = 4 + len(vp8_chunk)  # 4 = 'WEBP'
    return b"RIFF" + struct.pack("<I", total) + b"WEBP" + vp8_chunk


def test_probe_png() -> None:
    assert probe_image_dimensions(_fake_png(832, 1216)) == (832, 1216)


def test_probe_jpeg() -> None:
    assert probe_image_dimensions(_fake_jpeg(1024, 768)) == (1024, 768)


def test_probe_gif() -> None:
    assert probe_image_dimensions(_fake_gif(400, 300)) == (400, 300)


def test_probe_webp_vp8() -> None:
    assert probe_image_dimensions(_fake_webp_vp8(500, 500)) == (500, 500)


def test_probe_unknown_format_returns_none() -> None:
    assert probe_image_dimensions(b"not an image blob") is None


def test_probe_too_short_returns_none() -> None:
    assert probe_image_dimensions(b"\x89PNG") is None


def test_probe_truncated_png_returns_none() -> None:
    # Valid signature but body chopped before IHDR dims
    assert probe_image_dimensions(b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR") is None


# ---------- decision: photo vs document ----------


def test_normal_dimensions_choose_photo() -> None:
    assert decide_photo_vs_document(_fake_png(1024, 1024)) == "photo"
    assert decide_photo_vs_document(_fake_png(832, 1216)) == "photo"


def test_oversized_sum_degrades_to_document(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 9000 + 2000 = 11000 > 10000 → document
    with caplog.at_level("WARNING"):
        assert decide_photo_vs_document(_fake_png(9000, 2000)) == "document"
    assert any("sum=" in r.message for r in caplog.records)


def test_exact_sum_boundary_still_photo() -> None:
    # Inclusive boundary: 10000 is allowed per openclaw's implementation.
    total = MAX_PHOTO_DIMENSION_SUM
    assert decide_photo_vs_document(_fake_png(total // 2, total // 2)) == "photo"


def test_extreme_aspect_ratio_degrades(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # 100 × 3000 → ratio 30 > 20 → document
    with caplog.at_level("WARNING"):
        assert decide_photo_vs_document(_fake_png(100, 3000)) == "document"
    assert any("aspect ratio" in r.message for r in caplog.records)


def test_aspect_ratio_at_boundary_still_photo() -> None:
    # 100 × 2000 → ratio 20 = limit → photo (inclusive)
    assert decide_photo_vs_document(_fake_png(100, 2000)) == "photo"


def test_unparseable_defaults_to_photo(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Unknown format: we do NOT degrade. Keeps existing behaviour
    (let TG decide) but logs an info breadcrumb for ops."""
    with caplog.at_level("INFO"):
        assert decide_photo_vs_document(b"garbage") == "photo"
    assert any("unparseable header" in r.message for r in caplog.records)


def test_non_positive_dimensions_degrade(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING"):
        assert decide_photo_vs_document(_fake_png(0, 0)) == "document"
    assert any("non-positive" in r.message for r in caplog.records)


def test_log_context_flows_to_records(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The ``log_context`` string (usually chat_ref / url) must show up
    in every degrade log so ops can tie the warning back to a user."""
    with caplog.at_level("WARNING"):
        decide_photo_vs_document(
            _fake_png(9000, 2000),
            log_context="chat_ref=123 url=https://x/y.png",
        )
    assert any(
        "chat_ref=123" in r.message for r in caplog.records
    )
