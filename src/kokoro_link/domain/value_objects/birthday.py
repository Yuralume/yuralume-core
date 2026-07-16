"""Birthday-derived attributes (age, zodiac, days-until-next-birthday).

Pure helpers — no I/O, no clock dependencies. Callers always pass an
``as_of`` reference date so behaviour is deterministic in tests and
the same call can be used both for "real-time" age (wall clock) and
"in-universe" age (a world-bound character whose local time differs).

The zodiac table uses Gregorian western signs (牡羊 / 金牛 / ...).
Date ranges follow the conventional cutoffs; leap-day Pisces falls
naturally because the table is keyed on (month, day_inclusive_lower)
boundaries.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date


# (start_month, start_day, name). Each window runs from its start
# (inclusive) up to (but not including) the next entry's start; the
# wrap-around at year boundary is handled in ``zodiac_sign``.
_ZODIAC_WINDOWS: tuple[tuple[int, int, str], ...] = (
    (1, 20, "水瓶座"),
    (2, 19, "雙魚座"),
    (3, 21, "牡羊座"),
    (4, 20, "金牛座"),
    (5, 21, "雙子座"),
    (6, 22, "巨蟹座"),
    (7, 23, "獅子座"),
    (8, 23, "處女座"),
    (9, 23, "天秤座"),
    (10, 24, "天蠍座"),
    (11, 23, "射手座"),
    (12, 22, "摩羯座"),
)


def zodiac_sign(dob: date) -> str:
    """Return the western zodiac sign for ``dob`` in Chinese.

    The earliest window (摩羯座) wraps around Jan 1 – Jan 19; we encode
    that by treating "before the first explicit window" as the prior
    year's tail entry. Pure lookup — no astronomy adjustments.
    """
    month, day = dob.month, dob.day
    current = "摩羯座"  # default for dates before the first window of the year
    for start_month, start_day, name in _ZODIAC_WINDOWS:
        if (month, day) >= (start_month, start_day):
            current = name
        else:
            break
    return current


def compute_age(dob: date, as_of: date) -> int:
    """Whole-year age. Returns 0 for ``as_of`` before the birth date,
    matching the natural intuition "not born yet"."""
    if as_of < dob:
        return 0
    age = as_of.year - dob.year
    # Subtract one if the birthday hasn't occurred yet this year. Using
    # tuple comparison handles leap-day edge cleanly (Feb 29 birthday in
    # a non-leap year only "happens" on Mar 1 by Python's date math, so
    # we treat it as "next day" — matches Taiwan civil convention).
    if (as_of.month, as_of.day) < (dob.month, dob.day):
        age -= 1
    return age


def _next_birthday(dob: date, as_of: date) -> date:
    """Date of the next birthday occurrence on/after ``as_of``.

    Leap-day birthdays roll forward to Mar 1 in non-leap years rather
    than skipping a year — keeps "天還剩幾天" honest for Feb 29 babies."""
    year = as_of.year
    candidate = _try_birthday_in_year(dob, year)
    if candidate < as_of:
        candidate = _try_birthday_in_year(dob, year + 1)
    return candidate


def _try_birthday_in_year(dob: date, year: int) -> date:
    try:
        return date(year, dob.month, dob.day)
    except ValueError:
        # Feb 29 in a non-leap year — observe on Mar 1.
        return date(year, 3, 1)


def days_until_next_birthday(dob: date, as_of: date) -> int:
    """Whole days from ``as_of`` to the next birthday (0 = today)."""
    return (_next_birthday(dob, as_of) - as_of).days


def is_birthday_today(dob: date, as_of: date) -> bool:
    """True when ``as_of`` is the character's birthday occurrence.

    Same Feb-29-as-Mar-1 convention as :func:`_next_birthday` so a
    leap-baby still gets exactly one birthday per civil year.
    """
    observed = _try_birthday_in_year(dob, as_of.year)
    return observed == as_of


@dataclass(frozen=True, slots=True)
class BirthdayContext:
    """Bundle of birthday-derived attributes for ``as_of``.

    Built once per request by callers (prompt builder, feed candidate
    collector) so each downstream consumer can read the same numbers
    without recomputing — and so tests can stub the bundle directly.
    """

    dob: date
    age: int
    zodiac: str
    days_until_next: int
    is_today: bool

    @classmethod
    def from_date(cls, dob: date, as_of: date) -> "BirthdayContext":
        return cls(
            dob=dob,
            age=compute_age(dob, as_of),
            zodiac=zodiac_sign(dob),
            days_until_next=days_until_next_birthday(dob, as_of),
            is_today=is_birthday_today(dob, as_of),
        )
