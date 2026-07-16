"""``holidays``-package-backed CalendarContextPort implementation.

Wraps the third-party ``holidays`` library so the rest of the codebase
never has to import it directly. Region is operator-configured (default
``TW`` for Taiwan); switching region only requires changing one env var,
not touching any prompt code.

The ``holidays`` package lazily computes a country's calendar per year
on first access — we eagerly warm a window around today on init so the
hot path (one ``describe()`` call per schedule plan / prompt build) is
a cheap dict lookup.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone, tzinfo

import holidays as holidays_pkg

from kokoro_link.contracts.calendar_context import CalendarContextPort
from kokoro_link.infrastructure.calendar.facts import build_calendar_facts


_LOGGER = logging.getLogger(__name__)

# Default human-readable labels for regions we explicitly want to name
# in the prompt. Anything else falls back to the region code itself,
# which is still informative ("國定假日（JP）" beats no label at all).
_REGION_LABELS: dict[str, str] = {
    "TW": "台灣",
    "JP": "日本",
    "HK": "香港",
    "CN": "中國大陸",
    "US": "美國",
    "KR": "南韓",
    "SG": "新加坡",
}


class HolidaysCalendarProvider(CalendarContextPort):
    """Concrete adapter built on the ``holidays`` PyPI package."""

    def __init__(
        self,
        *,
        region: str = "TW",
        local_tz: tzinfo = timezone.utc,
        years_window: int = 2,
    ) -> None:
        normalised = (region or "TW").strip().upper() or "TW"
        self._region = normalised
        self._local_tz = local_tz
        self._years_window = years_window
        self._holidays_by_region: dict[str, holidays_pkg.HolidayBase] = {}
        self._holidays_by_region[normalised] = self._build_holiday_calendar(
            normalised, years_window,
        )

    @staticmethod
    def _build_holiday_calendar(
        region: str, years_window: int,
    ) -> holidays_pkg.HolidayBase:
        """Build the ``holidays`` country calendar, falling back to an
        empty calendar if the region code is unknown to the package.

        We intentionally don't raise here — an unknown region label is
        an operator-facing config issue, not a fatal error. The chat /
        schedule paths still work; they just lose the holiday layer for
        this character's region until the env is corrected.
        """
        this_year = datetime.now(timezone.utc).year
        # Cover [this_year - 1, this_year + years_window] so a midnight-
        # boundary plan_day call doesn't fall off the loaded range.
        years = list(range(this_year - 1, this_year + max(1, years_window) + 1))
        try:
            return holidays_pkg.country_holidays(region, years=years)
        except (NotImplementedError, KeyError, AttributeError):
            _LOGGER.warning(
                "Unknown holiday region '%s' — calendar adapter will "
                "report weekdays/weekends only.",
                region,
            )
            return holidays_pkg.HolidayBase(years=years)

    @property
    def region(self) -> str:
        return self._region

    def describe(self, today: date | None = None, *, region: str | None = None) -> str:
        target = today or self._today()
        resolved_region = _normalise_region(region) or self._region
        holidays_ = self._calendar_for_region(resolved_region)
        facts = build_calendar_facts(
            today=target,
            holidays_=holidays_,
            region_label=_REGION_LABELS.get(resolved_region, resolved_region),
        )
        return facts.to_prompt_block()

    def _calendar_for_region(self, region: str) -> holidays_pkg.HolidayBase:
        cached = self._holidays_by_region.get(region)
        if cached is not None:
            return cached
        built = self._build_holiday_calendar(region, self._years_window)
        self._holidays_by_region[region] = built
        return built

    def _today(self) -> date:
        """Compute the civil date in the configured local timezone.

        Mirrors :meth:`ScheduleService.today` so a planner call that
        omits the date lands on the same civil day the rest of the
        schedule layer is using.
        """
        moment = datetime.now(timezone.utc).astimezone(self._local_tz)
        return moment.date()


class NullCalendarProvider(CalendarContextPort):
    """Fallback for environments / tests that don't want a real calendar.

    Always returns an empty string so callers can splice it in without
    any conditional logic — empty prompt block = no calendar context.
    """

    def describe(
        self, today: date, *, region: str | None = None,
    ) -> str:  # pragma: no cover - trivial
        _ = today, region
        return ""


def _normalise_region(raw: str | None) -> str | None:
    if raw is None:
        return None
    region = raw.strip().upper()
    return region or None
