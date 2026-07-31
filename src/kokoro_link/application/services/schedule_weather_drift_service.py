"""Intra-day weather-drift vetting of a planned day.

A day is planned once — in the morning, against whatever sky happened to
be true then. By the afternoon the rain has stopped, the typhoon warning
lifted, or the forecast simply missed, yet the character keeps narrating
the sentence the planner wrote hours ago ("外面還在下雨，窩在家裡"), and
every downstream surface (chat prompt, proactive decider, feed composer)
reads that same stale line.

This service is the *correction at the source*: on every tick it takes
the block happening now plus the next couple, hands them to
:class:`ScheduleWeatherDriftPort` together with the weather facts that
are true at this instant, and applies the partial rewrites that come
back. Per the project's top directive there is no weather-category diff
and no "if rain then indoor" table here — Python only decides *which*
blocks are eligible and *when* it is worth paying for a verdict; whether
一段 河堤散步 contradicts a downpour (and 看電影 doesn't) is the model's
call alone.

Three rules keep the correction safe:

- **Only what is still ahead, only what is near, and only what is ours to
  change.** Finished and memorialised blocks already happened; a block
  starting hours out will meet a different sky than the one we are
  holding; an encounter is a real meeting with another character, and
  every operator-involvement block (confirmed plan, pending invite,
  voiced wish) is something the player was told. None of those may be
  silently reworded.
- **Cheap when nothing changed.** The ``(weather_vet_activity_id,
  weather_vet_condition)`` pair on the day is a pure cost gate: an
  unchanged pair means this block was already judged under this sky, so
  the tick skips the call. Either half changing re-opens it — which is
  exactly the "same activity, sky turned mid-block" case the old
  once-a-midnight refresh could never catch.
- **Never write back a stale day.** The judge takes seconds and the same
  civil day is written by the memorialiser, the encounter runner and the
  chat commitment extractor meanwhile. The verdict is merged onto a
  *freshly re-read* day, and the common "nothing contradicted" outcome
  touches only the two marker columns instead of replacing the day.

Everything is fail-soft: this runs inside the proactive tick, so a
missing profile, a dead weather upstream, or a crashing judge degrades to
"leave the day exactly as planned" and never propagates.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from datetime import date as date_cls, datetime, timedelta, timezone, tzinfo

from kokoro_link.application.services.location_context import (
    weather_location_from_operator,
)
from kokoro_link.contracts.schedule_repository import ScheduleRepositoryPort
from kokoro_link.contracts.schedule_weather_drift import (
    ActivityWeatherRewrite,
    ScheduleWeatherDriftPort,
)
from kokoro_link.contracts.weather_context import WeatherContextPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    OPERATOR_INVOLVEMENT_ROLES,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.timezone import timezone_for_id
from kokoro_link.infrastructure.weather.facts import extract_condition_phrase

_LOGGER = logging.getLogger(__name__)

_WINDOW_SIZE = 3
"""當前段 + 其後兩段。

Wide enough that a turning sky reaches the blocks the character will
actually narrate next (a tick is minutes, a block is an hour or two),
narrow enough that the judge reads a handful of lines instead of the
whole day."""

_WINDOW_HORIZON = timedelta(hours=4)
"""How far ahead a block may start and still be judged against *this* sky.

The count alone is not a bound: on an idle morning the "next three blocks"
can reach into the evening, and rewriting a 20:00 block against 09:30 rain
welds the current weather into it — then the gate stamps it as vetted, so
it keeps that morning prose right through the evening it describes. That
is the exact bug this service exists to remove, so blocks starting beyond
the horizon are simply left alone until they come within it."""

_ENCOUNTER_PARTNER_ROLE = "encounter_partner"

_PROTECTED_ROLES = frozenset({_ENCOUNTER_PARTNER_ROLE}) | OPERATOR_INVOLVEMENT_ROLES
"""Participant roles that put a block off-limits to a silent rewrite.

An encounter is a two-sided fact with another character. The three operator
involvement roles are all things the player was *told*: a confirmed shared
plan, a pending invite the character is still waiting on an answer for, and
a voiced wish. Rewording any of them behind the player's back leaves the
conversation she had and the schedule she keeps disagreeing, so the whole
:data:`OPERATOR_INVOLVEMENT_ROLES` set is excluded rather than a hand-picked
subset that drifts as new roles are added."""

_DEFAULT_PRIMARY_LANGUAGE = "zh-TW"


class ScheduleWeatherDriftService:
    """Re-reads today's remaining blocks against right-now weather facts."""

    def __init__(
        self,
        *,
        schedule_repository: ScheduleRepositoryPort,
        drift_port: ScheduleWeatherDriftPort | None,
        local_tz: tzinfo,
        weather_context_port: WeatherContextPort | None = None,
        operator_profile_service=None,  # noqa: ANN001 — optional owner resolver
    ) -> None:
        self._schedule_repository = schedule_repository
        # Both ports are optional so a deployment on the fake provider (or
        # without a weather upstream) simply never vets — same opt-in shape
        # as the aftermath port on the memorialiser.
        self._drift_port = drift_port
        self._local_tz = local_tz
        self._weather_context_port = weather_context_port
        self._operator_profile_service = operator_profile_service

    async def vet(
        self,
        character: Character,
        *,
        now: datetime | None = None,
    ) -> DailySchedule | None:
        """Vet today's remaining blocks; returns the day if it was saved.

        ``None`` means "nothing to do" — no port wired, no planned day, no
        block ahead, no weather facts, the gate was already satisfied, or
        anything failed. The caller (the tick) treats every one of those
        the same way: leave the day as planned.
        """
        if self._drift_port is None or self._weather_context_port is None:
            return None
        moment = _as_utc(now)
        try:
            return await self._vet(character, moment)
        except Exception:
            # The tick must survive anything unexpected in here.
            _LOGGER.exception(
                "schedule weather drift vet crashed character=%s", character.id,
            )
            return None

    async def _vet(
        self, character: Character, moment: datetime,
    ) -> DailySchedule | None:
        operator = await self._resolve_operator_profile(character)
        local_tz = _timezone_of(operator, self._local_tz)
        local_today = moment.astimezone(local_tz).date()
        schedule = await self._load_today(character, local_today)
        if schedule is None:
            return None
        window = _select_window(schedule, moment)
        if not window:
            return None
        weather_context = await self._describe_weather(operator)
        if not weather_context:
            return None
        condition = extract_condition_phrase(weather_context)
        if _already_vetted(schedule, window[0], condition):
            return None
        rewrites = await self._judge(
            character=character,
            window=window,
            weather_context=weather_context,
            moment=moment,
            local_tz=local_tz,
            operator=operator,
        )
        if rewrites is None:
            # The judge never spoke — leave the gate open so the next tick
            # retries rather than recording a verdict that never happened.
            return None
        return await self._land(
            character=character,
            local_today=local_today,
            window=window,
            rewrites=rewrites,
            head_id=window[0].id,
            condition=condition,
        )

    async def _land(
        self,
        *,
        character: Character,
        local_today: date_cls,
        window: tuple[ScheduleActivity, ...],
        rewrites: tuple[ActivityWeatherRewrite, ...],
        head_id: str,
        condition: str,
    ) -> DailySchedule | None:
        """Merge the verdict onto the day **as it stands now**.

        The snapshot handed to the judge is seconds old by the time it
        answers, and plenty else writes to the same day in that window: the
        memorialiser's exactly-once ``claim_memorialize`` CAS, an encounter
        block appended by the background runner, a commitment extracted from a
        chat turn. Persisting the pre-call snapshot would silently revert all
        of it (``apply_schedule_to_row`` replaces the whole activity
        collection), so the day is re-read and only the patches that still
        apply are laid on top.
        """
        fresh = await self._load_today(character, local_today)
        if fresh is None:
            # Deleted or regenerated mid-call — there is nothing to correct and
            # certainly nothing to resurrect.
            return None
        patched = _plan_patches(fresh, window=window, rewrites=rewrites)
        if not patched:
            # Nothing to rewrite: the marker alone goes down, as a targeted
            # two-column update rather than a whole-day replace.
            return await self._stamp(fresh, head_id, condition)
        vetted = fresh.with_activities([
            patched.get(activity.id, activity) for activity in fresh.activities
        ]).with_weather_vet(head_id, condition)
        return await self._save(vetted)

    async def _load_today(
        self, character: Character, local_today,  # noqa: ANN001
    ) -> DailySchedule | None:
        """Today's fully-planned day, or ``None``.

        A seed-only day (``is_planned=False``) holds nothing but a
        chat-extracted commitment — there is no planner prose to correct,
        and rewriting a commitment is exactly what we must not do."""
        try:
            schedule = await self._schedule_repository.get(
                character.id, local_today,
            )
        except Exception:
            _LOGGER.exception(
                "schedule weather drift: schedule load failed character=%s",
                character.id,
            )
            return None
        if schedule is None or not schedule.is_planned:
            return None
        return schedule if schedule.activities else None

    async def _describe_weather(self, operator: OperatorProfile | None) -> str:
        if self._weather_context_port is None:
            return ""
        try:
            return await self._weather_context_port.describe(
                location=weather_location_from_operator(operator),
            )
        except Exception:
            _LOGGER.exception("schedule weather drift: weather describe failed")
            return ""

    async def _judge(
        self,
        *,
        character: Character,
        window: tuple[ScheduleActivity, ...],
        weather_context: str,
        moment: datetime,
        local_tz: tzinfo,
        operator: OperatorProfile | None,
    ) -> tuple[ActivityWeatherRewrite, ...] | None:
        """Ask the port for rewrites; ``None`` when the call itself failed."""
        if self._drift_port is None:
            return None
        try:
            return tuple(
                await self._drift_port.judge(
                    character=character,
                    activities=window,
                    weather_context=weather_context,
                    now=moment,
                    local_tz=local_tz,
                    operator_primary_language=_operator_language(operator),
                ),
            )
        except Exception:
            _LOGGER.exception(
                "schedule weather drift: judge crashed character=%s", character.id,
            )
            return None

    async def _save(self, schedule: DailySchedule) -> DailySchedule | None:
        try:
            await self._schedule_repository.save(schedule)
        except Exception:
            _LOGGER.exception(
                "schedule weather drift: save failed character=%s date=%s",
                schedule.character_id, schedule.date,
            )
            return None
        return schedule

    async def _stamp(
        self, schedule: DailySchedule, head_id: str, condition: str,
    ) -> DailySchedule | None:
        """Record "asked about this block under this sky" and nothing else.

        Stamped even though nothing was rewritten — that is exactly what makes
        the next tick free."""
        try:
            await self._schedule_repository.set_weather_vet(
                schedule.character_id, schedule.date,
                activity_id=head_id, condition=condition,
            )
        except Exception:
            _LOGGER.exception(
                "schedule weather drift: vet stamp failed character=%s date=%s",
                schedule.character_id, schedule.date,
            )
            return None
        return schedule.with_weather_vet(head_id, condition)

    async def _resolve_operator_profile(
        self, character: Character,
    ) -> OperatorProfile | None:
        service = self._operator_profile_service
        if service is None:
            return None
        user_id = getattr(character, "user_id", None) or "default"
        try:
            return await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return None


def _as_utc(now: datetime | None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _timezone_of(operator: OperatorProfile | None, fallback: tzinfo) -> tzinfo:
    if operator is None:
        return fallback
    try:
        return timezone_for_id(getattr(operator, "timezone_id", None))
    except Exception:  # pragma: no cover - defensive
        return fallback


def _operator_language(operator: OperatorProfile | None) -> str:
    if operator is None:
        return _DEFAULT_PRIMARY_LANGUAGE
    lang = (getattr(operator, "primary_language", "") or "").strip()
    return lang or _DEFAULT_PRIMARY_LANGUAGE


def _select_window(
    schedule: DailySchedule, moment: datetime,
) -> tuple[ScheduleActivity, ...]:
    """The block happening now (or the next one) plus the following two.

    Bounded on three sides: blocks that already ended are out (rewriting
    them would rewrite history), blocks starting past
    :data:`_WINDOW_HORIZON` are out (this sky says nothing about theirs),
    and so is anything the character does not get to silently change on
    her own (see :func:`_is_protected`).
    """
    horizon = moment + _WINDOW_HORIZON
    eligible = [
        activity for activity in schedule.activities
        if activity.end_at > moment
        and activity.start_at <= horizon
        and not _is_protected(activity)
    ]
    return tuple(eligible[:_WINDOW_SIZE])


def _is_protected(activity: ScheduleActivity) -> bool:
    if activity.memorialized:
        # Already swept into an episodic memory — rewriting the prose now
        # would leave the day and the memory of it telling different stories.
        return True
    return any(ref.role in _PROTECTED_ROLES for ref in activity.participant_refs)


def _already_vetted(
    schedule: DailySchedule, head: ScheduleActivity, condition: str,
) -> bool:
    if not condition:
        # No condition phrase means there is nothing to compare against, so a
        # stamped marker proves nothing about the current sky — re-judge.
        return False
    return (
        schedule.weather_vet_activity_id == head.id
        and schedule.weather_vet_condition == condition
    )


def _plan_patches(
    schedule: DailySchedule,
    *,
    window: tuple[ScheduleActivity, ...],
    rewrites: tuple[ActivityWeatherRewrite, ...],
) -> dict[str, ScheduleActivity]:
    """The patched activities to lay onto ``schedule``, keyed by activity id.

    ``schedule`` is the freshly re-read day, so this is also where a verdict
    stops being applicable: an id that left the day, or that a concurrent
    runner memorialised / turned into a shared commitment while the judge was
    thinking, is dropped rather than forced through. Only ids inside the window
    are honoured (a judge that invents an activity gets dropped), and every
    field is a patch — a blank string or ``None`` means "the planner's value
    stands". The time skeleton and participants are not patchable at all: a
    weather rewrite changes what she is doing, never when or with whom.

    Patches that resolve to no change at all are left out so a verdict of
    "keep" everywhere still lands as a marker-only stamp.
    """
    window_ids = {activity.id for activity in window}
    by_id = {
        rewrite.activity_id: rewrite for rewrite in rewrites
        if rewrite.activity_id in window_ids
    }
    if not by_id:
        return {}
    patched: dict[str, ScheduleActivity] = {}
    for activity in schedule.activities:
        rewrite = by_id.get(activity.id)
        if rewrite is None or _is_protected(activity):
            continue
        updated = _patched(activity, rewrite)
        if updated != activity:
            patched[activity.id] = updated
    return patched


def _patched(
    activity: ScheduleActivity, rewrite: ActivityWeatherRewrite,
) -> ScheduleActivity:
    description = (rewrite.description or "").strip() or activity.description
    category = (rewrite.category or "").strip() or activity.category
    location = (rewrite.location or "").strip() or activity.location
    # A rewrite may move 露天座位 indoors. ``scene_privacy`` /
    # ``meeting_affordance`` were the planner's reading of the OLD setting, so
    # keeping them would pin the vanished outdoor semantics onto an indoor
    # block. They are cleared on a real change instead of re-derived here:
    # "unknown" lets any narrative reader infer from the new prose, which is
    # where the semantics belong.
    moved = description != activity.description or location != activity.location
    # ``replace`` (not a hand-rolled constructor call) so a field added to
    # ScheduleActivity later carries over instead of silently resetting to its
    # default; the entity re-clamps ``busy_score`` on the way through.
    return replace(
        activity,
        description=description,
        category=category,
        location=location,
        busy_score=(
            activity.busy_score if rewrite.busy_score is None
            else rewrite.busy_score
        ),
        scene_privacy=None if moved else activity.scene_privacy,
        meeting_affordance=None if moved else activity.meeting_affordance,
    )
