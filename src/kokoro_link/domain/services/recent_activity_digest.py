"""What the character已經排過／做過 in the civil days just gone (CF5).

The planner used to have no memory of adjacent days at all. Its only
"what has been going on" input was a summary of recent chat — which is
mostly the character's own proactive messages coming back, i.e. a closed
loop. The observable failure: one one-off activity ("重看某部作品的第 4
話") landed on three separate days in the same rolling-window pass, and a
handful of motifs cycled forever.

This module builds the missing input: a per-day list of activity
descriptions for the days immediately before the one being planned. It
is deliberately a **flat projection** — a date and the descriptions in
clock order, nothing else:

- No similarity comparison, no keyword extraction, no de-duplication
  across days. Deciding "is today's idea a repeat of Tuesday's?" is a
  semantic judgement and belongs to the planner LLM under the rules in
  ``schedule/planner.txt``; doing it here would be exactly the hardcoded
  matching the project's top directive forbids.
- No filtering by activity kind. The planner needs to see the routine
  entries too, because the anti-repetition rule explicitly exempts them —
  hiding them would leave the model guessing what counts as routine.

The anchor is the day *being planned*, not wall-clock today. The rolling
window pre-plans several days in one pass, so day N+1 must be able to see
day N's freshly-stored plan; anchoring on today would have left the whole
future half of the window blind to itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date as date_type, timedelta

from kokoro_link.domain.entities.schedule import DailySchedule

RECENT_ACTIVITY_LOOKBACK_DAYS = 7
"""How many civil days before the planned day are surfaced.

CF5 shipped with 3 (one rolling window); SE1 widened it to 7 because the
observed motif cycle is weekly, not per-window — a one-off that came back
every four to five days sat just outside a 3-day digest and read as fresh.
Seven civil days is the span the planner is asked to diversify against
(STORY_SEED_ENRICHMENT_PLAN §3 SE1); the per-day description cap keeps the
wider window's prompt weight bounded.
"""

MAX_ACTIVITIES_PER_RECENT_DAY = 12
"""Cap per day so a pathological row can't dominate the prompt. A planned
day tops out near ``_MAX_ACTIVITIES`` (14) anyway, and the tail of a day
is sleep."""


@dataclass(frozen=True, slots=True)
class RecentDayActivities:
    """One past day as the planner sees it: a date and what was on it."""

    date: date_type
    descriptions: tuple[str, ...]


def recent_activity_dates(
    target: date_type,
    *,
    lookback_days: int = RECENT_ACTIVITY_LOOKBACK_DAYS,
) -> tuple[date_type, ...]:
    """The civil days immediately preceding ``target``, oldest first.

    ``target`` itself is excluded — it is the day being replaced.
    """
    return tuple(
        target - timedelta(days=offset)
        for offset in range(lookback_days, 0, -1)
    )


def summarize_recent_day(
    schedule: DailySchedule | None,
) -> RecentDayActivities | None:
    """Project one stored day into its planner-facing digest.

    ``None`` for a missing day and for a day that holds no describable
    activity, so "nothing happened" never reaches the prompt as an empty
    bullet the model has to interpret.
    """
    if schedule is None:
        return None
    descriptions = tuple(
        description
        for description in (
            activity.description.strip()
            for activity in sorted(
                schedule.activities, key=lambda a: a.start_at,
            )
        )
        if description
    )[:MAX_ACTIVITIES_PER_RECENT_DAY]
    if not descriptions:
        return None
    return RecentDayActivities(
        date=schedule.date, descriptions=descriptions,
    )
