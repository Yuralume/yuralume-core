"""Pure calendar-fact derivation.

This module is intentionally library-and-IO-free at the call sites that
matter: the ``CalendarFacts`` value object is built from a small
``HolidayLookup`` protocol, so unit tests can substitute an in-memory
dict instead of pulling the ``holidays`` package's whole catalogue.

The split also keeps Chinese-prose rendering (``facts.to_prompt_block``)
separate from the structured fact extraction, so callers that only
want the structured data (e.g. a future API endpoint) don't have to
parse strings back out.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Protocol


_WEEKDAY_LABELS = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]

# How far we look in either direction for "recent past" / "upcoming"
# holiday context. 14 days covers the typical use of "上週末是XX節" or
# "下週五就是XX連假" without dragging context from months away.
_LOOKAHEAD_DAYS = 14
_LOOKBACK_DAYS = 14

# Cap on the length of a consecutive holiday run we'll annotate.
# Most TW 連假 are 2–6 days; anything longer than this is almost
# certainly a data quirk and we'd rather degrade gracefully than emit
# weird "連假第 47 天" lines.
_MAX_RUN_LENGTH = 10


class HolidayLookup(Protocol):
    """Minimal interface the facts builder needs from a holiday catalog.

    Compatible with ``holidays.HolidayBase`` (which behaves like a dict
    keyed by date) and any in-memory ``dict[date, str]`` we wire in
    tests.
    """

    def get(self, key: date) -> str | None: ...

    def __contains__(self, item: object) -> bool: ...


def _season_label(month: int) -> str:
    """Coarse season label.

    We use a four-season civil split — close enough for prompt cues
    ("初夏", "盛夏") without dragging in solar-term lookup tables.
    """
    if month in (3, 4, 5):
        return "春季"
    if month in (6, 7, 8):
        return "夏季"
    if month in (9, 10, 11):
        return "秋季"
    return "冬季"


def _is_weekend(d: date) -> bool:
    return d.weekday() >= 5


def _is_off_day(d: date, holidays_: HolidayLookup) -> bool:
    """A day is "off" if it's a weekend OR a recognised holiday."""
    return _is_weekend(d) or d in holidays_


@dataclass(frozen=True, slots=True)
class HolidayRun:
    """A consecutive run of off-days containing ``today``.

    ``length`` ≥ 2 is what humans usually call 連假. When length == 1
    (e.g. an isolated holiday surrounded by workdays, or just one
    weekend day with weekdays around it for some odd region) we still
    return the run but callers can suppress the "連假" framing.
    """

    start: date
    end: date
    length: int
    position: int  # 1-indexed day within the run
    contains_named_holiday: bool


def _detect_run(today: date, holidays_: HolidayLookup) -> HolidayRun | None:
    """Walk backwards then forwards from ``today`` to find the maximal
    consecutive off-day run that includes today. Returns ``None`` if
    today itself is a normal workday."""
    if not _is_off_day(today, holidays_):
        return None
    start = today
    while True:
        prev = start - timedelta(days=1)
        if (today - prev).days > _MAX_RUN_LENGTH:
            break
        if not _is_off_day(prev, holidays_):
            break
        start = prev
    end = today
    while True:
        nxt = end + timedelta(days=1)
        if (nxt - today).days > _MAX_RUN_LENGTH:
            break
        if not _is_off_day(nxt, holidays_):
            break
        end = nxt
    length = (end - start).days + 1
    position = (today - start).days + 1
    contains_named = any(
        (start + timedelta(days=i)) in holidays_ for i in range(length)
    )
    return HolidayRun(
        start=start,
        end=end,
        length=length,
        position=position,
        contains_named_holiday=contains_named,
    )


@dataclass(frozen=True, slots=True)
class NearbyHoliday:
    when: date
    name: str
    days_away: int  # positive = future, negative = past


def _find_nearest_future_holiday(
    today: date, holidays_: HolidayLookup,
) -> NearbyHoliday | None:
    """Scan forward up to ``_LOOKAHEAD_DAYS`` days for the next named
    holiday strictly after ``today``."""
    for i in range(1, _LOOKAHEAD_DAYS + 1):
        candidate = today + timedelta(days=i)
        name = holidays_.get(candidate)
        if name:
            return NearbyHoliday(when=candidate, name=name, days_away=i)
    return None


def _find_nearest_past_holiday(
    today: date, holidays_: HolidayLookup,
) -> NearbyHoliday | None:
    """Scan backward up to ``_LOOKBACK_DAYS`` days for the most recent
    named holiday strictly before ``today``."""
    for i in range(1, _LOOKBACK_DAYS + 1):
        candidate = today - timedelta(days=i)
        name = holidays_.get(candidate)
        if name:
            return NearbyHoliday(when=candidate, name=name, days_away=-i)
    return None


@dataclass(frozen=True, slots=True)
class CalendarFacts:
    """Structured calendar context for a single civil date.

    Built by :func:`build_calendar_facts` and rendered to natural-
    language by :meth:`to_prompt_block`. Splitting structure from prose
    keeps the rendering tweakable without retesting the fact derivation.
    """

    today: date
    weekday_label: str
    is_weekend: bool
    holiday_name: str | None
    run: HolidayRun | None
    next_holiday: NearbyHoliday | None
    last_holiday: NearbyHoliday | None
    season_label: str
    region_label: str

    @property
    def is_holiday(self) -> bool:
        return self.holiday_name is not None

    @property
    def is_workday(self) -> bool:
        return not (self.is_weekend or self.is_holiday)

    def to_prompt_block(self) -> str:
        lines = [
            f"今天是 {self.today.isoformat()}（{self.weekday_label}）。",
        ]
        # Day-class line
        if self.is_holiday:
            lines.append(f"今天是國定假日「{self.holiday_name}」（{self.region_label}）。")
        elif self.is_weekend:
            lines.append("今天是週末。")
        else:
            lines.append("今天是平日（工作日）。")
        # Run framing — only if it's a genuine multi-day off stretch
        if self.run and self.run.length >= 2 and self.run.contains_named_holiday:
            lines.append(
                f"這是一個 {self.run.length} 天連假的第 {self.run.position} 天"
                f"（連假期間 {self.run.start.isoformat()}–{self.run.end.isoformat()}）。"
            )
            if self.run.position == 1:
                lines.append("（連假首日）")
            elif self.run.position == self.run.length:
                lines.append("（連假最後一天，明天恢復上班/上課）")
        # Neighbour context
        if self.next_holiday is not None:
            lines.append(
                f"下一個國定假日：{self.next_holiday.when.isoformat()}"
                f"「{self.next_holiday.name}」（{self.next_holiday.days_away} 天後）。"
            )
        if self.last_holiday is not None:
            past_days = -self.last_holiday.days_away
            lines.append(
                f"上一個國定假日：{self.last_holiday.when.isoformat()}"
                f"「{self.last_holiday.name}」（{past_days} 天前）。"
            )
        lines.append(f"時節：{self.season_label}。")
        lines.append(
            "請依角色身分（學生／上班族／自由工作者…）與性格自行判斷今天的生活節奏，"
            "不要假設所有角色作息相同。"
        )
        return "\n".join(lines)


def build_calendar_facts(
    *,
    today: date,
    holidays_: HolidayLookup,
    region_label: str,
) -> CalendarFacts:
    """Assemble structured calendar facts for ``today``.

    ``region_label`` is a human-readable region name (e.g. "台灣") that
    gets surfaced in the prompt so the LLM can sanity-check whether
    the holiday list applies to this character's setting.
    """
    weekday_label = _WEEKDAY_LABELS[today.weekday()]
    holiday_name = holidays_.get(today)
    return CalendarFacts(
        today=today,
        weekday_label=weekday_label,
        is_weekend=_is_weekend(today),
        holiday_name=holiday_name,
        run=_detect_run(today, holidays_),
        next_holiday=_find_nearest_future_holiday(today, holidays_),
        last_holiday=_find_nearest_past_holiday(today, holidays_),
        season_label=_season_label(today.month),
        region_label=region_label,
    )
