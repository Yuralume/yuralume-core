"""BDD for LINE image URL pre-flight validation.

Covers the pure function side. Adapter-level integration (degrade to
text note + log warning) lives in ``test_line_adapter_attachments.py``.
"""

from __future__ import annotations

import pytest

from kokoro_link.infrastructure.messaging.line.url_validation import (
    LineUrlValidationError,
    validate_line_image_url,
)


# ---------- happy path ----------


def test_https_png_passes() -> None:
    validate_line_image_url("https://cdn.example.com/a.png")


def test_https_jpg_passes() -> None:
    validate_line_image_url("https://cdn.example.com/a.jpg")


def test_https_jpeg_passes() -> None:
    validate_line_image_url("https://cdn.example.com/a.jpeg")


def test_path_with_query_and_fragment_is_fine() -> None:
    validate_line_image_url(
        "https://cdn.example.com/folder/sub/a.png?v=1&sig=abc#frag",
    )


def test_uppercase_extension_accepted() -> None:
    """Case-insensitive: LINE accepts ``IMAGE.PNG`` in the path too."""
    validate_line_image_url("https://cdn.example.com/IMAGE.PNG")


# ---------- rejection paths ----------


def test_http_scheme_rejected() -> None:
    with pytest.raises(LineUrlValidationError) as info:
        validate_line_image_url("http://cdn.example.com/a.png")
    assert "https" in info.value.reason
    assert info.value.url == "http://cdn.example.com/a.png"


def test_missing_scheme_rejected() -> None:
    with pytest.raises(LineUrlValidationError) as info:
        validate_line_image_url("cdn.example.com/a.png")
    assert "https" in info.value.reason


def test_empty_url_rejected() -> None:
    with pytest.raises(LineUrlValidationError) as info:
        validate_line_image_url("")
    assert "empty" in info.value.reason


def test_whitespace_only_url_rejected() -> None:
    with pytest.raises(LineUrlValidationError):
        validate_line_image_url("   ")


def test_oversized_url_rejected() -> None:
    long_url = "https://cdn.example.com/" + ("x" * 2100) + ".png"
    with pytest.raises(LineUrlValidationError) as info:
        validate_line_image_url(long_url)
    assert "length" in info.value.reason
    assert "2000" in info.value.reason


def test_webp_extension_rejected() -> None:
    """LINE silently 400s on WebP even over HTTPS; fail loud instead."""
    with pytest.raises(LineUrlValidationError) as info:
        validate_line_image_url("https://cdn.example.com/a.webp")
    assert ".webp" in info.value.reason


def test_gif_extension_rejected() -> None:
    with pytest.raises(LineUrlValidationError) as info:
        validate_line_image_url("https://cdn.example.com/a.gif")
    assert ".gif" in info.value.reason


def test_no_extension_rejected() -> None:
    with pytest.raises(LineUrlValidationError) as info:
        validate_line_image_url("https://cdn.example.com/a")
    assert "none" in info.value.reason or "." in info.value.reason


def test_no_host_rejected() -> None:
    with pytest.raises(LineUrlValidationError) as info:
        validate_line_image_url("https:///a.png")
    assert "host" in info.value.reason


def test_error_preserves_original_url() -> None:
    """The raised error always carries the full URL so operators can
    grep logs without relying on surrounding context."""
    with pytest.raises(LineUrlValidationError) as info:
        validate_line_image_url("http://x.com/a.png")
    assert info.value.url == "http://x.com/a.png"
    assert "http://x.com/a.png" in str(info.value)
