"""Shared relative-time label helpers (HUMANIZATION_ROADMAP §4.4 timing).

These back the memory recall tags, feed/proactive recall material, and
the chat history day-boundary separators. We pin the bucket boundaries
and the "約" hedging so the wording stays coarse — the LLM should read a
recall anchor, not a precise number it might echo literally.
"""

from datetime import datetime, timezone

from zoneinfo import ZoneInfo

from kokoro_link.infrastructure.prompt.timing_utils import (
    format_civil_days_ago_label,
    format_gap_duration_label,
    format_relative_past_label,
    render_date_anchor_lines,
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


# ------------------------------------------------------------------
# Date-anchor table (COMMITMENT_LIFECYCLE_AND_FRESHNESS P3)
# ------------------------------------------------------------------


def test_date_anchor_spans_two_days_back_and_three_forward() -> None:
    lines = render_date_anchor_lines(
        datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc), timezone.utc,
    )
    assert lines[0].startswith("日期換算錨點")
    assert lines[1:] == [
        "- 前天＝2026-07-27（星期一）",
        "- 昨天＝2026-07-28（星期二）",
        "- 今天＝2026-07-29（星期三）",
        "- 明天＝2026-07-30（星期四）",
        "- 後天＝2026-07-31（星期五）",
        "- 大後天＝2026-08-01（星期六）",
    ]


def test_date_anchor_resolves_the_civil_day_in_the_local_timezone() -> None:
    """23:30 UTC is already the next civil day in Taipei — the anchor
    must agree with the operator's calendar, not UTC's."""
    lines = render_date_anchor_lines(
        datetime(2026, 6, 19, 23, 30, tzinfo=timezone.utc),
        ZoneInfo("Asia/Taipei"),
    )
    assert "- 今天＝2026-06-20（星期六）" in lines


def test_date_anchor_crosses_month_and_year_boundaries() -> None:
    lines = render_date_anchor_lines(
        datetime(2026, 12, 31, 12, 0, tzinfo=timezone.utc), timezone.utc,
    )
    assert "- 今天＝2026-12-31（星期四）" in lines
    assert "- 明天＝2027-01-01（星期五）" in lines


def test_date_anchor_localises_the_weekday_annotation() -> None:
    """The model copies this label into content written in the
    operator's language, so it follows the operator's language."""
    lines = render_date_anchor_lines(
        datetime(2026, 7, 29, 3, 0, tzinfo=timezone.utc),
        timezone.utc,
        language_tag="ja-JP",
    )
    assert "- 今天＝2026-07-29（水曜日）" in lines


def test_date_anchor_is_empty_without_a_reference_instant() -> None:
    assert render_date_anchor_lines(None, timezone.utc) == []


# ------------------------------------------------------------------
# Civil-day age label (COMMITMENT_LIFECYCLE_AND_FRESHNESS P2d)
# ------------------------------------------------------------------


def test_civil_days_ago_counts_calendar_days_not_elapsed_hours() -> None:
    """A goal written at 23:50 last night is "1 天前" even though barely
    eight hours have passed — the character talks about it in calendar
    terms, so the label has to agree with the calendar."""
    now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    last_night = datetime(2026, 7, 28, 23, 50, tzinfo=timezone.utc)
    assert format_civil_days_ago_label(last_night, now, local_tz=timezone.utc) == "1 天前"


def test_civil_days_ago_same_day_reads_as_today() -> None:
    now = datetime(2026, 7, 29, 20, 0, tzinfo=timezone.utc)
    this_morning = datetime(2026, 7, 29, 7, 0, tzinfo=timezone.utc)
    assert format_civil_days_ago_label(this_morning, now, local_tz=timezone.utc) == "今天"


def test_civil_days_ago_uses_the_operator_civil_day() -> None:
    """23:30 UTC is already the next day in Taipei; a stamp two hours
    later must not read as a day older than it is."""
    now = datetime(2026, 7, 29, 1, 30, tzinfo=timezone.utc)  # 09:30 Taipei 7/29
    written = datetime(2026, 7, 28, 23, 30, tzinfo=timezone.utc)  # 07:30 Taipei 7/29
    label = format_civil_days_ago_label(
        written, now, local_tz=ZoneInfo("Asia/Taipei"),
    )
    assert label == "今天"


def test_civil_days_ago_treats_naive_stamps_as_utc() -> None:
    now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    stored = datetime(2026, 7, 26, 8, 0)  # legacy naive row
    assert format_civil_days_ago_label(stored, now, local_tz=timezone.utc) == "3 天前"


def test_civil_days_ago_is_empty_when_unknowable_or_skewed() -> None:
    now = datetime(2026, 7, 29, 8, 0, tzinfo=timezone.utc)
    assert format_civil_days_ago_label(None, now, local_tz=timezone.utc) == ""
    assert format_civil_days_ago_label(now, None, local_tz=timezone.utc) == ""
    future = datetime(2026, 7, 30, 8, 0, tzinfo=timezone.utc)
    assert format_civil_days_ago_label(future, now, local_tz=timezone.utc) == ""
