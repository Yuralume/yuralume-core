"""Calendar-facts port.

The schedule planner and prompt builder both need to know "what is today
in real-world calendar terms" — is it a national holiday, a normal
weekday, the first day of a 連假, an exam-season weekend, etc. — so the
LLM can reflect those rhythms (學生今天不用上課 / 上班族 blue Monday /
連假最後一天的收心) when generating activities and dialogue.

We expose the calendar as a single ``describe(today)`` call that returns
a pre-rendered natural-language block. Keeping the surface this thin
lets us swap the underlying calendar library (currently ``holidays``)
without touching call sites, and means the prompt-side code never has
to know whether a date is a holiday — it just splices the block in.

**Important**: the port returns *facts only*. It must not tell the LLM
how the character should behave on a given date — that's the LLM's job
based on the character persona + the facts we provide. This keeps the
project's LLM-first principle intact (no if-else like
"if holiday then skip work block").
"""

from __future__ import annotations

from datetime import date
from typing import Protocol


class CalendarContextPort(Protocol):
    def describe(self, today: date, *, region: str | None = None) -> str:
        """Return a natural-language block describing ``today``'s calendar.

        The block typically covers:

        - ISO date + weekday label
        - whether today is a national holiday, a weekend, or a workday
        - the name of the holiday when applicable
        - position within a multi-day consecutive holiday run (連假第 N 天 / 末日)
        - nearest upcoming holiday within a short look-ahead
        - the most recent past holiday within a short look-back
        - season label (initial-spring / summer / etc.) derived from month

        Returns an empty string when no calendar provider is wired —
        callers should treat that as "no calendar context available"
        and render nothing rather than fabricating values.

        ``region`` overrides the provider's deployment fallback for this
        call. Passing ``None`` preserves the legacy env-configured
        calendar region.
        """
