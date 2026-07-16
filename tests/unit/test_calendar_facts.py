"""Tests for the calendar-fact derivation helpers.

The facts layer is pure and library-free (no ``holidays`` dependency in
this module): we feed it an in-memory ``dict[date, str]`` standing in
for a country calendar so each scenario stays explicit.
"""

from __future__ import annotations

from datetime import date

from kokoro_link.infrastructure.calendar.facts import (
    CalendarFacts,
    build_calendar_facts,
)


def _facts(today: date, holidays: dict[date, str]) -> CalendarFacts:
    return build_calendar_facts(
        today=today, holidays_=holidays, region_label="測試區",
    )


def test_plain_weekday_is_workday() -> None:
    # 2026-05-19 is a Tuesday with no holiday in our fixture.
    facts = _facts(date(2026, 5, 19), {})
    assert facts.weekday_label == "星期二"
    assert facts.is_workday is True
    assert facts.is_holiday is False
    assert facts.is_weekend is False
    block = facts.to_prompt_block()
    assert "2026-05-19" in block
    assert "星期二" in block
    assert "平日（工作日）" in block


def test_weekend_is_marked() -> None:
    # 2026-05-23 is a Saturday.
    facts = _facts(date(2026, 5, 23), {})
    assert facts.is_weekend is True
    assert facts.is_workday is False
    block = facts.to_prompt_block()
    assert "星期六" in block
    assert "週末" in block


def test_named_holiday_surfaces_name_and_region() -> None:
    facts = _facts(date(2026, 1, 1), {date(2026, 1, 1): "開國紀念日"})
    assert facts.is_holiday is True
    assert facts.holiday_name == "開國紀念日"
    block = facts.to_prompt_block()
    assert "開國紀念日" in block
    assert "測試區" in block


def test_three_day_run_first_day_is_flagged() -> None:
    # Fri-Sat-Sun where Friday is a holiday.
    holidays = {date(2026, 10, 9): "國慶日"}
    facts = _facts(date(2026, 10, 9), holidays)
    assert facts.run is not None
    assert facts.run.length == 3
    assert facts.run.position == 1
    assert facts.run.contains_named_holiday is True
    block = facts.to_prompt_block()
    assert "連假首日" in block
    assert "3 天連假" in block


def test_run_last_day_is_flagged() -> None:
    holidays = {date(2026, 10, 9): "國慶日"}
    # Sunday — last day of the Fri-Sat-Sun run.
    facts = _facts(date(2026, 10, 11), holidays)
    assert facts.run is not None
    assert facts.run.length == 3
    assert facts.run.position == 3
    block = facts.to_prompt_block()
    assert "連假最後一天" in block


def test_isolated_weekend_does_not_use_lianhuali_framing() -> None:
    # Saturday with no surrounding holidays — still a 2-day run (Sat+Sun)
    # but contains no named holiday, so we suppress the "連假" line.
    facts = _facts(date(2026, 5, 23), {})
    assert facts.run is not None
    assert facts.run.length == 2
    block = facts.to_prompt_block()
    assert "連假第" not in block
    assert "連假首日" not in block


def test_next_holiday_lookahead_within_two_weeks() -> None:
    holidays = {date(2026, 5, 25): "假日測試"}
    facts = _facts(date(2026, 5, 19), holidays)
    assert facts.next_holiday is not None
    assert facts.next_holiday.days_away == 6
    block = facts.to_prompt_block()
    assert "下一個國定假日" in block
    assert "假日測試" in block


def test_previous_holiday_lookback_within_two_weeks() -> None:
    holidays = {date(2026, 5, 12): "假日測試"}
    facts = _facts(date(2026, 5, 19), holidays)
    assert facts.last_holiday is not None
    assert facts.last_holiday.days_away == -7
    block = facts.to_prompt_block()
    assert "上一個國定假日" in block


def test_seasons_map_to_civil_quarters() -> None:
    assert _facts(date(2026, 4, 1), {}).season_label == "春季"
    assert _facts(date(2026, 7, 1), {}).season_label == "夏季"
    assert _facts(date(2026, 10, 1), {}).season_label == "秋季"
    assert _facts(date(2026, 1, 15), {}).season_label == "冬季"


def test_block_never_prescribes_behavior() -> None:
    """Per project CLAUDE.md: calendar layer delivers facts only, must
    not tell the LLM what the character should do on this date."""
    facts = _facts(date(2026, 1, 1), {date(2026, 1, 1): "開國紀念日"})
    block = facts.to_prompt_block()
    # We intentionally include a guard line that nudges the model to
    # reason from persona instead of assuming uniform behaviour.
    assert "依角色身分" in block
    # And we never inject prescriptive language like "today don't go to work".
    forbidden = ["不要上班", "不要上課", "請放假", "今天放假吧"]
    for phrase in forbidden:
        assert phrase not in block
