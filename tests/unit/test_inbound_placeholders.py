"""Unit tests for inbound attachment placeholder localization."""

from __future__ import annotations

from kokoro_link.infrastructure.messaging.inbound_placeholders import (
    ATTACHMENT_PLACEHOLDER,
    PHOTO_PLACEHOLDER,
    localize_inbound_placeholder_text,
)


def test_photo_placeholder_exact_match_localized() -> None:
    out = localize_inbound_placeholder_text(PHOTO_PLACEHOLDER, "en-US")
    assert "使用者" not in out
    assert "image" in out.lower()


def test_attachment_placeholder_exact_match_localized() -> None:
    out = localize_inbound_placeholder_text(ATTACHMENT_PLACEHOLDER, "ja-JP")
    assert "使用者" not in out


def test_placeholder_with_caption_preserves_caption() -> None:
    text = f"{PHOTO_PLACEHOLDER} check this out"
    out = localize_inbound_placeholder_text(text, "en-US")
    assert out.endswith("check this out")
    assert "使用者" not in out


def test_zh_operator_keeps_canonical_placeholder() -> None:
    assert (
        localize_inbound_placeholder_text(PHOTO_PLACEHOLDER, "zh-TW")
        == PHOTO_PLACEHOLDER
    )


def test_ordinary_text_unchanged() -> None:
    assert (
        localize_inbound_placeholder_text("just a message", "en-US")
        == "just a message"
    )


def test_empty_text_unchanged() -> None:
    assert localize_inbound_placeholder_text("", "en-US") == ""


def test_unknown_language_falls_back_to_zh() -> None:
    # fr-FR is unsupported → zh-TW canonical placeholder.
    assert (
        localize_inbound_placeholder_text(PHOTO_PLACEHOLDER, "fr-FR")
        == PHOTO_PLACEHOLDER
    )
