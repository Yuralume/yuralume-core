"""Unit tests for the pure WebP variant encoder (IV0).

Covers ``docs/plans/IMAGE_DELIVERY_AND_PAGINATION_PLAN.md`` §5 IV0
acceptance criteria: tier spec correctness (D1), never-upscale, fail-soft
on bad/truncated/empty input, alpha preservation, and the object-key
derivation rule that IV1 (storage decorator) and IV3 (backfill script)
must both reuse rather than reimplement.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from kokoro_link.infrastructure.storage.image_variants import (
    VARIANT_SPECS,
    EncodedVariant,
    ImageVariantSpec,
    derive_variant_key,
    encode_variants,
)
from kokoro_link.infrastructure.storage.keys import validate_object_key


def _png_bytes(
    width: int,
    height: int,
    *,
    mode: str = "RGB",
    color: tuple[int, int, int] = (200, 40, 40),
) -> bytes:
    image = Image.new(mode, (width, height), color)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _rgba_png_bytes(width: int, height: int, *, alpha: int) -> bytes:
    image = Image.new("RGBA", (width, height), (10, 20, 30, alpha))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _palette_png_with_transparency(width: int, height: int) -> bytes:
    """A P-mode PNG whose palette index 0 is fully transparent (tRNS)."""
    image = Image.new("P", (width, height))
    palette = [0, 0, 0] * 256
    palette[0:3] = [255, 0, 0]
    image.putpalette(palette)
    image.info["transparency"] = 0
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _decode(content: bytes) -> Image.Image:
    return Image.open(io.BytesIO(content))


# ---------- tier spec matches plan D1 ----------


def test_variant_specs_match_plan_d1() -> None:
    by_name = {spec.name: spec for spec in VARIANT_SPECS}
    assert len(VARIANT_SPECS) == 3
    assert by_name["w320"] == ImageVariantSpec(name="w320", target_width=320, quality=75)
    assert by_name["w768"] == ImageVariantSpec(name="w768", target_width=768, quality=82)
    assert by_name["full"] == ImageVariantSpec(name="full", target_width=None, quality=85)


# ---------- fail-soft: never raise ----------


def test_empty_bytes_returns_empty_dict() -> None:
    assert encode_variants(b"") == {}


def test_non_image_bytes_returns_empty_dict() -> None:
    assert encode_variants(b"this is definitely not an image, just prose") == {}


def test_truncated_png_returns_empty_dict() -> None:
    full = _png_bytes(1024, 1536)
    truncated = full[: len(full) // 2]
    assert encode_variants(truncated) == {}


def test_png_signature_only_returns_empty_dict() -> None:
    assert encode_variants(b"\x89PNG\r\n\x1a\n") == {}


def test_single_byte_returns_empty_dict() -> None:
    assert encode_variants(b"\x00") == {}


# ---------- never upscale ----------


def test_source_wider_than_all_tiers_produces_all_three() -> None:
    result = encode_variants(_png_bytes(1024, 1536))
    assert set(result) == {"w320", "w768", "full"}
    assert result["w320"].width == 320
    assert result["w768"].width == 768
    assert result["full"].width == 1024
    assert result["full"].height == 1536


def test_source_narrower_than_w768_skips_w768_only() -> None:
    # 500 wide: > 320 (w320 kept) but <= 768 (w768 skipped, never upscale)
    result = encode_variants(_png_bytes(500, 700))
    assert set(result) == {"w320", "full"}
    assert result["w320"].width == 320


def test_source_narrower_than_w320_skips_both_thumbnail_tiers() -> None:
    result = encode_variants(_png_bytes(200, 300))
    assert set(result) == {"full"}
    assert result["full"].width == 200
    assert result["full"].height == 300


def test_source_width_exactly_at_tier_boundary_is_skipped_not_upscaled() -> None:
    # source width == target width -> skip (spec: "<=" means skip, not 1:1).
    result = encode_variants(_png_bytes(320, 480))
    assert "w320" not in result
    assert set(result) == {"full"}


def test_full_variant_never_resizes() -> None:
    result = encode_variants(_png_bytes(50, 90))
    full = result["full"]
    assert (full.width, full.height) == (50, 90)
    decoded = _decode(full.content)
    assert decoded.size == (50, 90)


def test_aspect_ratio_preserved_on_resize() -> None:
    result = encode_variants(_png_bytes(1024, 1536))
    w320 = result["w320"]
    assert w320.width == 320
    assert w320.height == round(1536 * 320 / 1024)
    decoded = _decode(w320.content)
    assert decoded.size == (w320.width, w320.height)


# ---------- output shape ----------


def test_variants_are_valid_webp_bytes() -> None:
    result = encode_variants(_png_bytes(1024, 1536))
    assert result  # sanity: this source produces all three tiers
    for variant in result.values():
        assert isinstance(variant, EncodedVariant)
        assert variant.content_type == "image/webp"
        decoded = _decode(variant.content)
        assert decoded.format == "WEBP"


# ---------- alpha preservation ----------


def test_alpha_channel_preserved_for_rgba_source() -> None:
    result = encode_variants(_rgba_png_bytes(400, 400, alpha=77))
    decoded = _decode(result["full"].content).convert("RGBA")
    _, _, _, alpha = decoded.getpixel((0, 0))
    assert alpha == 77


def test_opaque_source_encodes_without_alpha() -> None:
    result = encode_variants(_png_bytes(400, 400))
    decoded = _decode(result["full"].content)
    assert decoded.mode == "RGB"


def test_palette_transparency_preserved() -> None:
    result = encode_variants(_palette_png_with_transparency(400, 400))
    decoded = _decode(result["full"].content).convert("RGBA")
    _, _, _, alpha = decoded.getpixel((0, 0))
    assert alpha == 0


# ---------- key derivation shared with IV1/IV3 ----------


def test_derive_variant_key_appends_dot_name_webp() -> None:
    assert (
        derive_variant_key("characters/abc/portrait.png", "w320")
        == "characters/abc/portrait.png.w320.webp"
    )


@pytest.mark.parametrize("name", ["w320", "w768", "full"])
def test_derive_variant_key_output_passes_object_key_validation(name: str) -> None:
    key = derive_variant_key("users/1/messaging-inbound/2024/photo.jpg", name)
    assert validate_object_key(key) == key


def test_derive_variant_key_rejects_empty_object_key() -> None:
    with pytest.raises(ValueError):
        derive_variant_key("", "w320")


def test_derive_variant_key_rejects_unknown_variant_name() -> None:
    with pytest.raises(ValueError):
        derive_variant_key("a/b.png", "w9999")
