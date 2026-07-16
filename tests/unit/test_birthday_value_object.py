"""Unit tests for the ``birthday`` value object helpers.

Pure-function tests — no fixtures, no I/O. Covers:

- Zodiac boundary days (cutoffs in the table).
- Leap-year Feb-29 birthdays (age computed from the year, "observe on
  Mar 1" rule for ``days_until_next_birthday`` / ``is_birthday_today``).
- Whole-year age rolling exactly on the birthday.
- ``BirthdayContext.from_date`` bundling for the prompt builder.
"""

from datetime import date

import pytest

from kokoro_link.domain.value_objects.birthday import (
    BirthdayContext,
    compute_age,
    days_until_next_birthday,
    is_birthday_today,
    zodiac_sign,
)


@pytest.mark.parametrize(
    ("dob", "expected"),
    [
        (date(1990, 1, 1), "摩羯座"),    # before first window → wraps to 摩羯
        (date(1990, 1, 19), "摩羯座"),   # last day before 水瓶
        (date(1990, 1, 20), "水瓶座"),   # first day of 水瓶
        (date(1990, 2, 18), "水瓶座"),
        (date(1990, 2, 19), "雙魚座"),
        (date(1990, 3, 20), "雙魚座"),
        (date(1990, 3, 21), "牡羊座"),
        (date(1990, 6, 21), "雙子座"),
        (date(1990, 6, 22), "巨蟹座"),
        (date(1990, 12, 21), "射手座"),
        (date(1990, 12, 22), "摩羯座"),
        (date(1990, 12, 31), "摩羯座"),
    ],
)
def test_zodiac_sign_boundaries(dob: date, expected: str) -> None:
    assert zodiac_sign(dob) == expected


def test_compute_age_before_birthday_in_year() -> None:
    dob = date(2000, 6, 15)
    as_of = date(2026, 6, 14)
    assert compute_age(dob, as_of) == 25


def test_compute_age_on_birthday() -> None:
    dob = date(2000, 6, 15)
    as_of = date(2026, 6, 15)
    assert compute_age(dob, as_of) == 26


def test_compute_age_after_birthday() -> None:
    dob = date(2000, 6, 15)
    as_of = date(2026, 6, 16)
    assert compute_age(dob, as_of) == 26


def test_compute_age_not_born_yet_returns_zero() -> None:
    dob = date(2030, 1, 1)
    as_of = date(2026, 1, 1)
    assert compute_age(dob, as_of) == 0


def test_compute_age_leap_baby_uses_mar1_in_non_leap_year() -> None:
    # Born Feb 29, 2000. On Feb 28 of a non-leap year the birthday
    # hasn't occurred yet (we observe on Mar 1).
    dob = date(2000, 2, 29)
    assert compute_age(dob, date(2026, 2, 28)) == 25
    # On Mar 1 of a non-leap year the observed birthday has happened.
    assert compute_age(dob, date(2026, 3, 1)) == 26
    # Same date in a leap year: Feb 29 is the real birthday.
    assert compute_age(dob, date(2024, 2, 29)) == 24


def test_days_until_next_birthday_today_returns_zero() -> None:
    dob = date(1995, 8, 12)
    assert days_until_next_birthday(dob, date(2026, 8, 12)) == 0


def test_days_until_next_birthday_wraps_year() -> None:
    dob = date(1995, 1, 5)
    # End of 2026 → next occurrence is Jan 5, 2027 (5 days).
    assert days_until_next_birthday(dob, date(2026, 12, 31)) == 5


def test_days_until_next_birthday_leap_baby_in_non_leap_year() -> None:
    dob = date(2000, 2, 29)
    # 2026 is not a leap year; observed birthday is Mar 1, 2026.
    assert days_until_next_birthday(dob, date(2026, 2, 27)) == 2


def test_is_birthday_today_exact_match() -> None:
    dob = date(1990, 4, 10)
    assert is_birthday_today(dob, date(2026, 4, 10)) is True
    assert is_birthday_today(dob, date(2026, 4, 9)) is False
    assert is_birthday_today(dob, date(2026, 4, 11)) is False


def test_is_birthday_today_leap_baby_observes_mar1() -> None:
    dob = date(2000, 2, 29)
    assert is_birthday_today(dob, date(2026, 3, 1)) is True
    assert is_birthday_today(dob, date(2026, 2, 28)) is False
    # In a leap year, Feb 29 is the true birthday.
    assert is_birthday_today(dob, date(2024, 2, 29)) is True
    assert is_birthday_today(dob, date(2024, 3, 1)) is False


def test_birthday_context_bundles_all_derivations() -> None:
    dob = date(2000, 6, 15)
    as_of = date(2026, 6, 10)
    ctx = BirthdayContext.from_date(dob, as_of)
    assert ctx.dob == dob
    assert ctx.age == 25
    assert ctx.zodiac == "雙子座"
    assert ctx.days_until_next == 5
    assert ctx.is_today is False


def test_birthday_context_flags_today() -> None:
    dob = date(2000, 6, 15)
    ctx = BirthdayContext.from_date(dob, date(2026, 6, 15))
    assert ctx.is_today is True
    assert ctx.days_until_next == 0
    assert ctx.age == 26
