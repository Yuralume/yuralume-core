"""Behaviour spec for the token estimator.

Pins the properties the budgeting code depends on, not exact numbers for
their own sake — the whole module is an approximation and a test that
demanded a specific count for a specific sentence would just be a
transcription of the implementation.

The properties that *are* load-bearing:

* monotonic — more text never estimates cheaper (a budget check that
  could shrink under added text is not a budget check);
* the CJK / non-CJK split is real, and full-width punctuation lands on
  the CJK side, because Traditional-Chinese prose is full of it;
* total, ceiling-rounded, never negative, never raises.
"""

from __future__ import annotations

import pytest

from kokoro_link.llm_output import estimate_tokens, estimate_total_tokens
from kokoro_link.llm_output.tokens import count_cjk_chars, is_cjk_char


# --- the character classifier -----------------------------------------


@pytest.mark.parametrize(
    "char",
    ["中", "文", "字", "あ", "ア", "한", "。", "、", "「", "」", "！", "？"],
)
def test_cjk_and_fullwidth_punctuation_are_billed_as_cjk(char: str) -> None:
    assert is_cjk_char(char)


@pytest.mark.parametrize("char", ["a", "Z", "0", " ", ".", ",", "!", "\n", "-"])
def test_ascii_is_not_billed_as_cjk(char: str) -> None:
    assert not is_cjk_char(char)


def test_empty_input_classifies_as_not_cjk() -> None:
    assert not is_cjk_char("")


def test_count_cjk_chars_over_mixed_text() -> None:
    assert count_cjk_chars("hello 世界") == 2


# --- the estimate ------------------------------------------------------


def test_empty_text_costs_nothing() -> None:
    assert estimate_tokens("") == 0


def test_any_non_empty_text_costs_at_least_one_token() -> None:
    """Ceiling rounding, and the reason for it: a budget must not be
    fooled by a fractional remainder that rounds to zero."""
    assert estimate_tokens("a") == 1
    assert estimate_tokens(" ") == 1


def test_cjk_is_one_token_per_character() -> None:
    assert estimate_tokens("今天天氣很好") == 6


def test_latin_is_a_quarter_token_per_character() -> None:
    assert estimate_tokens("a" * 40) == 10


def test_mixed_text_adds_the_two_sides() -> None:
    # 4 CJK + 8 ASCII -> 4 + 2
    assert estimate_tokens("今天天氣 is good") == pytest.approx(
        4 + (len("今天天氣 is good") - 4) / 4, abs=1,
    )


def test_estimate_is_monotonic_in_added_text() -> None:
    base = "他昨天說要去台中出差三天"
    for suffix in ("", "。", " ok", "，還問我要不要一起", "x" * 50):
        assert estimate_tokens(base + suffix) >= estimate_tokens(base)


def test_cjk_costs_more_per_character_than_latin() -> None:
    """The whole reason the estimator is not ``len / 4``: a
    Traditional-Chinese prompt of the same length is several times the
    tokens of an English one, and a budget built on ``len`` alone would
    be wrong by that factor for every conversation this product has."""
    assert estimate_tokens("一" * 100) > estimate_tokens("a" * 100) * 3


# --- the iterable helper ----------------------------------------------


def test_total_sums_each_element_separately() -> None:
    lines = ["你好", "hello", ""]
    assert estimate_total_tokens(lines) == sum(
        estimate_tokens(line) for line in lines
    )


def test_total_of_nothing_is_zero() -> None:
    assert estimate_total_tokens([]) == 0
    assert estimate_total_tokens(None) == 0


def test_total_accepts_a_bare_string() -> None:
    assert estimate_total_tokens("你好") == estimate_tokens("你好")


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "\n\n", "🙂🙂", "```json\n{}\n```", "ばか", "𠀀𠀁"],
)
def test_estimator_is_total(raw: str) -> None:
    """Never raises, never negative — including for astral-plane CJK
    extensions, emoji and whitespace-only input."""
    assert estimate_tokens(raw) >= 0
