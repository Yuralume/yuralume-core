"""Intra-day schedule weather-drift port.

A day is planned once, in the morning, against the weather that happened
to be true at midnight. By afternoon the rain has stopped, the typhoon
warning lifted, or the forecast simply missed — but the character keeps
narrating the plan she wrote hours ago ("外面還在下雨，窩在家裡"), and
every downstream surface (chat prompt, proactive decider, feed composer)
reads that same stale sentence.

This port asks the LLM to compare the **schedule text itself** against
the weather facts that are true *right now* and rewrite only the blocks
that plainly contradict them. Per the project's top directive there is no
weather-category diff and no "if rain then indoor" rule table: a film
screening is untouched by any weather change, a 河堤散步 under a fresh
downpour is not, and only the model can tell those apart. Blocks the
model considers untouched are simply absent from the result.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Protocol

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity


@dataclass(frozen=True, slots=True)
class ActivityWeatherRewrite:
    """A partial rewrite of one activity, keyed by ``activity_id``.

    Every field is a *patch*: the defaults mean "leave the activity's
    current value alone", never "blank it out". A judge that only wants
    to reword the description leaves ``location`` / ``category`` /
    ``busy_score`` as ``None`` and the applier keeps whatever the planner
    originally wrote.

    The activity's time skeleton (``start_at`` / ``end_at``) and its
    participants are deliberately not patchable — a weather rewrite may
    change *what she is doing*, never *when* or *with whom*, so a
    commitment the user was told about keeps its shape.

    ``scene_privacy`` / ``meeting_affordance`` are not patchable either,
    but they are not preserved: the applier clears them whenever the
    description or location actually changes, because the planner's
    reading of 露天座位 says nothing about the indoor corner the rewrite
    moved her to. Scene Access then treats them as unknown and infers
    from the new prose instead of applying a vanished outdoor verdict.
    """

    activity_id: str
    description: str = ""
    location: str | None = None
    category: str | None = None
    busy_score: float | None = None


class ScheduleWeatherDriftPort(Protocol):
    async def judge(
        self,
        *,
        character: Character,
        activities: tuple[ScheduleActivity, ...],
        weather_context: str,
        now: datetime,
        local_tz: tzinfo,
        operator_primary_language: str = "zh-TW",
    ) -> tuple[ActivityWeatherRewrite, ...]:
        """Return rewrites for the activities that contradict the weather.

        ``activities`` is the caller-selected window — the block happening
        now plus the next couple — already filtered of anything that must
        not be silently rewritten (completed blocks, encounters, plans the
        user confirmed). Judges must not invent entries outside it; the
        caller drops unknown ``activity_id`` values.

        ``weather_context`` is the pre-rendered weather fact block for
        *this moment* (``WeatherFacts.to_prompt_block()``). It is the only
        weather authority — the schedule's own prose is a planning-time
        narration and never evidence of the current sky.

        ``now`` / ``local_tz`` let the judge phrase the window in civil
        time and reason about how much of a block is still ahead.

        ``operator_primary_language`` (BCP 47) is the content language of
        the rewritten prose: the description lands in the schedule the
        player reads and every downstream prompt, so a non-Chinese
        operator must not get a Chinese sentence spliced into their day.

        Must be fail-soft: any internal error (model timeout, parse fail,
        empty response) returns an empty tuple rather than raising. An
        empty tuple means "nothing contradicts the weather" — the caller
        keeps the day exactly as planned.
        """
        ...
