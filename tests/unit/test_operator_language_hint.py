"""The one line every player-visible LLM job gets about output language.

A BCP 47 code alone is a weak signal for smaller models — ``zh-TW``
reliably produces Chinese but not reliably *Traditional* Chinese — so
the line also names the language and, where the language is written in
more than one script, demands one explicitly.
"""

from __future__ import annotations

import pytest

from kokoro_link.infrastructure.prompt.operator_language import (
    render_operator_language_hint,
    render_operator_language_lines,
)


def test_traditional_chinese_names_the_language_and_forbids_simplified() -> None:
    hint = render_operator_language_hint("zh-TW")
    assert "繁體中文（台灣）" in hint
    assert "BCP 47：zh-TW" in hint
    assert "一個簡體字都不能出現" in hint


def test_traditional_chinese_steers_regional_wording() -> None:
    """The deterministic normaliser converts glyphs but never vocabulary
    (``视频`` becomes ``視頻``, not ``影片``), so wording is the prompt's
    job and has to be stated here or nowhere."""
    hint = render_operator_language_hint("zh-TW")
    assert "影片" in hint
    assert "軟體" in hint


def test_simplified_chinese_players_are_not_pushed_to_traditional() -> None:
    hint = render_operator_language_hint("zh-CN")
    assert "简体中文" in hint
    assert "繁體字" not in hint


def test_non_chinese_languages_carry_no_script_demand() -> None:
    """A Chinese-script clause leaking into a Japanese or English prompt
    would be worse than saying nothing."""
    for tag, name in (("ja", "日本語"), ("en-US", "English"), ("ko", "한국어")):
        hint = render_operator_language_hint(tag)
        assert name in hint
        assert "簡體" not in hint
        assert "繁體" not in hint


@pytest.mark.parametrize("tag", ["zh-tw", "ZH-TW", "zh_TW", "  zh-TW  "])
def test_tag_matching_tolerates_case_and_separator(tag: str) -> None:
    assert "繁體中文（台灣）" in render_operator_language_hint(tag)


def test_unknown_tag_falls_back_to_the_bare_code() -> None:
    """An unfamiliar language still gets a correct instruction, just a
    weaker one — never a wrong one."""
    hint = render_operator_language_hint("fr-CA")
    assert "fr-CA（BCP 47 標籤）" in hint
    assert "必須使用此語言" in hint


def test_missing_tag_renders_nothing() -> None:
    assert render_operator_language_hint("") == ""
    assert render_operator_language_hint(None) == ""
    assert render_operator_language_lines(None) == []
    assert len(render_operator_language_lines("zh-TW")) == 1
