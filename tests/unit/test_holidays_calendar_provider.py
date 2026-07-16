"""Tests for the ``holidays``-package-backed CalendarContextPort adapter.

These tests do hit the real ``holidays`` library because that's the
unit under test — the fact-derivation logic is exercised independently
in ``test_calendar_facts.py``.
"""

from __future__ import annotations

from datetime import date

from kokoro_link.infrastructure.calendar.holidays_provider import (
    HolidaysCalendarProvider,
    NullCalendarProvider,
)


def test_tw_new_years_day_lights_up_as_holiday() -> None:
    provider = HolidaysCalendarProvider(region="TW")
    block = provider.describe(date(2026, 1, 1))
    assert "中華民國開國紀念日" in block
    assert "台灣" in block


def test_tw_regular_tuesday_renders_workday() -> None:
    provider = HolidaysCalendarProvider(region="TW")
    block = provider.describe(date(2026, 5, 19))
    assert "平日（工作日）" in block
    assert "星期二" in block


def test_tw_dragonboat_renders_with_seasonal_marker() -> None:
    provider = HolidaysCalendarProvider(region="TW")
    block = provider.describe(date(2026, 6, 19))
    assert "端午節" in block
    # 6 月 → 夏季 in our civil-quarter scheme
    assert "夏季" in block


def test_unknown_region_falls_back_quietly() -> None:
    """Operators sometimes mistype the region code. We must not crash —
    the prompt path should still produce a usable block (weekday-only
    catalog) so chat / schedule keep working.
    """
    provider = HolidaysCalendarProvider(region="ZZ")
    block = provider.describe(date(2026, 5, 19))
    assert "2026-05-19" in block
    assert "星期二" in block


def test_describe_can_override_region_per_call() -> None:
    provider = HolidaysCalendarProvider(region="TW")

    tw_block = provider.describe(date(2026, 1, 1))
    us_block = provider.describe(date(2026, 1, 1), region="US")

    assert "台灣" in tw_block
    assert "美國" in us_block
    assert "New Year's Day" in us_block


def test_null_provider_always_returns_empty_string() -> None:
    provider = NullCalendarProvider()
    assert provider.describe(date(2026, 5, 19)) == ""
