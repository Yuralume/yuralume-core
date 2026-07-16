"""Shared relative-time label helpers (HUMANIZATION_ROADMAP §4.4 timing).

These back the memory recall tags, feed/proactive recall material, and
the chat history day-boundary separators. We pin the bucket boundaries
and the "約" hedging so the wording stays coarse — the LLM should read a
recall anchor, not a precise number it might echo literally.
"""

from kokoro_link.infrastructure.prompt.timing_utils import (
    format_gap_duration_label,
    format_relative_past_label,
)


def test_relative_past_collapses_sub_two_minutes_to_just_now() -> None:
    assert format_relative_past_label(0.0) == "剛剛"
    assert format_relative_past_label(1.9) == "剛剛"


def test_relative_past_minutes_bucket() -> None:
    assert format_relative_past_label(30) == "約 30 分鐘前"


def test_relative_past_hours_bucket() -> None:
    assert format_relative_past_label(3 * 60 + 20) == "約 3 小時前"


def test_relative_past_days_bucket() -> None:
    # 6/24 event seen on 6/26 -> ~2 days -> the canonical "約 2 天前".
    assert format_relative_past_label(2 * 24 * 60) == "約 2 天前"


def test_relative_past_weeks_then_months() -> None:
    assert format_relative_past_label(10 * 24 * 60) == "約 1 週前"
    assert format_relative_past_label(60 * 24 * 60) == "約 2 個月前"


def test_gap_duration_has_no_trailing_suffix() -> None:
    # Duration label (for "中間隔了 X") omits the 前 the past-label adds.
    assert format_gap_duration_label(16 * 60) == "約 16 小時"
    assert format_gap_duration_label(2 * 24 * 60) == "約 2 天"


def test_gap_duration_minutes_round() -> None:
    assert format_gap_duration_label(45) == "約 45 分鐘"
