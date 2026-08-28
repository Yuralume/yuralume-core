"""QG0 mechanical evidence helpers (D6).

The single property worth defending here is the one the LLM-first red line
turns on: these functions **describe**, they never decide. So every test
asserts on what the evidence *says*, and none asserts that anything was
blocked, trimmed or rewritten — because none of that is these functions'
to do.
"""

from __future__ import annotations

from kokoro_link.application.services.output_quality import (
    length_overrun_lines,
    script_mix_lines,
)


# ── length overrun ───────────────────────────────────────────────────


def test_overrun_reports_length_cap_and_the_suspicion() -> None:
    lines = length_overrun_lines("x" * 300, 280)

    assert len(lines) == 1
    line = lines[0]
    assert "300" in line and "280" in line
    assert "20" in line  # the excess, so the model knows how much to cut
    assert "非正文" in line


def test_text_inside_the_cap_produces_no_evidence() -> None:
    assert length_overrun_lines("x" * 280, 280) == ()
    assert length_overrun_lines("短", 280) == ()


def test_no_text_or_no_cap_produces_no_evidence() -> None:
    """Callers splice this into an evidence tuple unconditionally, so every
    degenerate input has to be an empty tuple rather than a raise."""
    assert length_overrun_lines("", 280) == ()
    assert length_overrun_lines("x" * 300, 0) == ()
    assert length_overrun_lines("x" * 300, -1) == ()


# ── script mix ───────────────────────────────────────────────────────


def test_pure_chinese_reads_as_pure_chinese() -> None:
    lines = script_mix_lines(["今天下午的雨停了", "我在窗邊坐了很久"])

    assert len(lines) == 1  # nothing crossed the per-line report threshold
    assert "中日韓 100%" in lines[0]
    assert "近 2 則輸出" in lines[0]


def test_mixed_script_lines_are_named_individually() -> None:
    lines = script_mix_lines(
        [
            "今天下午的雨停了",
            "真的 super amazing 到 unbelievable level",
        ],
        primary_language="zh-TW",
    )

    assert len(lines) == 2
    assert "拉丁字母" in lines[0]
    assert "第 2 則" in lines[1]
    assert "第 1 則" not in lines[1]


def test_digits_and_punctuation_do_not_count_as_a_script() -> None:
    """A line of "……！？123" is not a language shift, and would read as one
    if the denominator counted every character."""
    lines = script_mix_lines(["今天，下午 3:45……真的！？"])

    assert "中日韓 100%" in lines[0]


def test_japanese_and_korean_count_as_cjk() -> None:
    lines = script_mix_lines(["ひらがなカタカナ", "한글입니다"])

    assert "中日韓 100%" in lines[0]


def test_empty_input_produces_no_evidence() -> None:
    assert script_mix_lines([]) == ()
    assert script_mix_lines(["", "   "]) == ()
    assert script_mix_lines(["123", "!!!"]) == ()


def test_lines_with_no_letters_still_count_towards_the_total() -> None:
    """A blank-ish line is dropped, but a numeric one is a real output and
    is counted in the "近 N 則" denominator the judge reads."""
    lines = script_mix_lines(["今天下午的雨停了", "123"])

    assert "近 2 則輸出" in lines[0]


def test_the_per_line_detail_is_capped() -> None:
    lines = script_mix_lines(["all latin here"] * 8, primary_language="zh-TW")

    assert "第 6 則" not in lines[1]
    assert "另有 3 則" in lines[1]


# ── the mix is measured *against the operator's own script* ──────────
#
# Without a language reference "mixed" collapses into "contains Latin",
# which is the normal state of every reply an en/vi/id/es operator ever
# receives. The share reported is therefore the share of the script the
# operator does *not* write in.


def test_latin_operator_reading_pure_latin_sees_no_mix() -> None:
    """The regression this parameter exists for.

    Every reply to an English-speaking operator is 100% Latin. Reported as
    a mix, it makes the chat risk score fire on every single turn, which
    costs that operator token-by-token streaming forever."""
    lines = script_mix_lines(
        ["It rained all afternoon.", "I sat by the window for a while."],
        primary_language="en",
    )

    assert len(lines) == 1  # the composition summary, and nothing flagged
    assert "拉丁字母 100%" in lines[0]


def test_latin_operator_reading_cjk_output_is_flagged() -> None:
    """Same direction, mirrored: for an en operator the *foreign* script is
    CJK, so a reply that drifted into Chinese is the one worth naming."""
    lines = script_mix_lines(
        ["It rained all afternoon.", "今天下午一直在下雨，我坐在窗邊。"],
        primary_language="en",
    )

    assert len(lines) == 2
    assert "中日韓文字" in lines[1]
    assert "第 2 則" in lines[1]
    assert "第 1 則" not in lines[1]


def test_cjk_operator_reading_pure_latin_is_still_flagged() -> None:
    """A zh operator whose character answered entirely in English is the
    original 語言不符 case and must keep firing."""
    lines = script_mix_lines(
        ["今天下午的雨停了", "It rained all afternoon and then it stopped."],
        primary_language="zh-TW",
    )

    assert len(lines) == 2
    assert "拉丁字母" in lines[1]
    assert "第 2 則" in lines[1]


def test_language_tag_matching_ignores_region_case_and_separator() -> None:
    mixed = ["今天下午的雨停了", "真的 super amazing 到 unbelievable level"]

    for tag in ("zh", "zh-TW", "zh_Hant_TW", "ZH-CN", "ja-JP", "ko", "yue"):
        assert len(script_mix_lines(mixed, primary_language=tag)) == 2, tag


def test_an_unknown_or_absent_language_keeps_the_cjk_reference() -> None:
    """The default has to be the pre-existing behaviour, or every caller
    that has no operator language to hand changes meaning silently."""
    mixed = ["今天下午的雨停了", "真的 super amazing 到 unbelievable level"]

    assert script_mix_lines(mixed) == script_mix_lines(
        mixed, primary_language="zh-TW",
    )
    assert script_mix_lines(mixed, primary_language="   ") == script_mix_lines(
        mixed, primary_language="zh-TW",
    )


# ── the "other" script family (Thai, Russian, Arabic, …) ───────────────
#
# Under the old CJK-vs-Latin split, an operator writing in a script that is
# neither fell through to the Latin default: their own text landed in the
# "other" bucket, but so did any *actual* foreign-script drift into CJK,
# and a drift into Latin (English) was silently treated as home and never
# flagged at all. The fix gives these operators a real reference script.


def test_thai_operator_reading_pure_thai_sees_no_mix() -> None:
    lines = script_mix_lines(
        ["วันนี้ฝนตกทั้งบ่าย", "ฉันนั่งอยู่ริมหน้าต่างนานมาก"],
        primary_language="th",
    )

    assert len(lines) == 1  # nothing crossed the per-line report threshold


def test_thai_operator_drifting_into_english_is_flagged() -> None:
    """The regression this batch exists to fix: measured against Latin
    (the old default for any non-CJK tag), this drift read as 0% foreign
    and the signal vanished entirely for every other-script operator."""
    lines = script_mix_lines(
        ["วันนี้ฝนตกทั้งบ่าย", "It rained all afternoon and then it stopped."],
        primary_language="th",
    )

    assert len(lines) == 2
    assert "拉丁字母或中日韓文字" in lines[1]
    assert "第 2 則" in lines[1]
    assert "第 1 則" not in lines[1]


def test_russian_and_arabic_operators_get_the_same_other_reference() -> None:
    for tag, native, drift in (
        (
            "ru", "Дождь шёл весь день, и я долго сидел у окна.",
            "It rained all afternoon and then it stopped, believe it or not.",
        ),
        (
            "ar", "أمطرت طوال فترة بعد الظهر وجلست عند النافذة طويلا",
            "It rained all afternoon and then it stopped, believe it or not.",
        ),
    ):
        pure = script_mix_lines([native, native], primary_language=tag)
        assert len(pure) == 1, tag

        drifted = script_mix_lines([native, drift], primary_language=tag)
        assert len(drifted) == 2, tag
        assert "第 2 則" in drifted[1], tag


# ── ISO 639-2/T three-letter tags for the CJK languages ────────────────
#
# normalise_language_tag() accepts any 2-3 letter subtag, so a profile
# stored with the three-letter form (jpn/kor/zho/chi) must resolve to the
# same CJK reference as its two-letter (ja/ko/zh) counterpart, not silently
# fall through to the Latin default and reproduce the original 語言不符
# blind spot for every operator whose tag happens to be three letters.


def test_three_letter_cjk_tags_are_recognised() -> None:
    pure_english = ["It rained all afternoon.", "I sat by the window for a while."]

    for three_letter, two_letter in (
        ("jpn", "ja"),
        ("kor", "ko"),
        ("zho", "zh"),
        ("chi", "zh"),
    ):
        assert script_mix_lines(
            pure_english, primary_language=three_letter,
        ) == script_mix_lines(
            pure_english, primary_language=two_letter,
        ), three_letter
