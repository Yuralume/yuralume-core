"""Unit tests for the deterministic per-locale fallback text table.

This module is the shared backend-composed-text localizer (NOT an LLM
path). Behaviour under test: exact-tag lookup, language-subtag family
fallback, zh-TW ship-first fallback, and named-parameter formatting.
"""

from __future__ import annotations

import pytest

from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
    resolve_fallback_language,
)


class TestResolveFallbackLanguage:
    def test_exact_tag_matches(self) -> None:
        assert resolve_fallback_language("en-US") == "en-US"
        assert resolve_fallback_language("ja-JP") == "ja-JP"
        assert resolve_fallback_language("zh-TW") == "zh-TW"

    def test_language_subtag_family_falls_back(self) -> None:
        # en-GB is not a shipped tag but the en family resolves to en-US.
        assert resolve_fallback_language("en-GB") == "en-US"
        assert resolve_fallback_language("ja") == "ja-JP"

    def test_unknown_or_empty_falls_back_to_zh_tw(self) -> None:
        assert resolve_fallback_language("fr-FR") == "zh-TW"
        assert resolve_fallback_language("") == "zh-TW"
        assert resolve_fallback_language(None) == "zh-TW"


class TestLocalizedFallbackText:
    def test_chat_apology_localized_per_language(self) -> None:
        zh = localized_fallback_text("chat.tool_truncated_apology", "zh-TW")
        en = localized_fallback_text("chat.tool_truncated_apology", "en-US")
        ja = localized_fallback_text("chat.tool_truncated_apology", "ja-JP")
        assert "抱歉" in zh
        # en/ja must not leak the Chinese apology
        assert "抱歉" not in en
        assert "Sorry" in en
        assert "抱歉" not in ja
        assert "ごめん" in ja

    def test_named_param_formatting(self) -> None:
        out = localized_fallback_text(
            "channel.line.attachment_label", "en-US", label="report.pdf",
        )
        assert out == "Attachment: report.pdf"

    def test_missing_language_falls_back_to_zh_tw_text(self) -> None:
        # Unknown language → zh-TW string, params still applied.
        out = localized_fallback_text(
            "channel.line.attachment_label", "fr-FR", label="x",
        )
        assert out == "附件：x"

    def test_inbound_placeholders_localized(self) -> None:
        assert (
            localized_fallback_text("inbound.photo_placeholder", "zh-TW")
            == "[使用者傳來一張圖片]"
        )
        en = localized_fallback_text("inbound.photo_placeholder", "en-US")
        assert "使用者" not in en and "image" in en.lower()
        ja = localized_fallback_text("inbound.attachment_placeholder", "ja-JP")
        assert "使用者" not in ja

    def test_unknown_key_raises(self) -> None:
        with pytest.raises(KeyError):
            localized_fallback_text("nope.not_a_key", "zh-TW")

    def test_every_key_has_zh_tw_baseline(self) -> None:
        # Guard: the guaranteed fallback locale must never be missing.
        from kokoro_link.infrastructure.localization.fallback_texts import (
            _FALLBACK_LANGUAGE,
            _TEXTS,
        )

        for key, catalog in _TEXTS.items():
            assert _FALLBACK_LANGUAGE in catalog, key

    def test_every_key_has_full_trilingual_parity(self) -> None:
        # Guard: player-visible fallbacks must ship all three UI locales
        # so en/ja operators never silently fall back to Chinese text.
        from kokoro_link.infrastructure.localization.fallback_texts import (
            _SUPPORTED_LANGUAGES,
            _TEXTS,
        )

        for key, catalog in _TEXTS.items():
            for language in _SUPPORTED_LANGUAGES:
                assert language in catalog, f"{key} missing {language}"

    def test_weekday_labels_localized_and_complete(self) -> None:
        from kokoro_link.infrastructure.localization.fallback_texts import (
            _WEEKDAY_LABELS,
            localized_weekday_label,
        )

        for language, labels in _WEEKDAY_LABELS.items():
            assert len(labels) == 7, language
        # Monday index 0 differs per locale; en/ja must not be Chinese.
        assert localized_weekday_label(0, "zh-TW") == "星期一"
        assert localized_weekday_label(0, "en-US") == "Monday"
        assert localized_weekday_label(0, "ja-JP") == "月曜日"
        # Unknown tag falls back to the zh-TW table.
        assert localized_weekday_label(6, "fr-FR") == "星期日"

    def test_encounter_fallbacks_localized(self) -> None:
        # The plan (#5/#6) fallbacks reach the player through the
        # relationships panel + memory browser — they must localise.
        en_loc = localized_fallback_text("encounter.default_location", "en-US")
        assert "日常" not in en_loc and "route" in en_loc.lower()
        ja_summary = localized_fallback_text(
            "encounter.summary_met", "ja-JP", location="cafe", name="Rin",
        )
        assert "短暫碰面" not in ja_summary and "Rin" in ja_summary
        en_activity = localized_fallback_text(
            "encounter.schedule_activity", "en-US", name="Mei",
        )
        assert "碰面" not in en_activity and "Mei" in en_activity
