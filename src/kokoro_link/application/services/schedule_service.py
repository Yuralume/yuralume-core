"""Schedule application service.

Lazily generates a character's ``DailySchedule`` on first use per civil
date. Used by ``ChatService`` to enrich prompts with what the character
is currently doing and what's up next.

Design choices:

- Resolves the character owner's fixed user timezone for civil dates and
  local activity times. The constructor ``local_tz`` is only a legacy
  fallback for tests or unwired services.
- ``ensure_schedule`` is idempotent: once a schedule for a date exists,
  it is returned as-is. Force refresh goes through ``regenerate``.
- The planner produces the schedule; the service only wires it to the
  repository. That keeps the LLM/Stub switch isolated.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING

from kokoro_link.application.dto.schedule import (
    CurrentActivityResponse,
    DailyScheduleResponse,
)
from kokoro_link.application.services.location_context import (
    calendar_region_from_operator,
    weather_location_from_operator,
)
from kokoro_link.contracts.behavioral_pattern import (
    BehavioralPatternRepositoryPort,
)
from kokoro_link.contracts.calendar_context import CalendarContextPort
from kokoro_link.contracts.dialogue_summarizer import DialogueSummarizerPort
from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.contracts.weather_context import WeatherContextPort
from kokoro_link.contracts.post_turn import ScheduleAdjustment
from kokoro_link.contracts.repositories import ConversationRepositoryPort
from kokoro_link.contracts.schedule_planner import SchedulePlannerPort
from kokoro_link.contracts.schedule_repository import ScheduleRepositoryPort
from kokoro_link.domain.entities.behavioral_pattern import (
    KIND_RECURRING_ACTIVITY,
    KIND_TIME_PREFERENCE,
    BehavioralPattern,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    OPERATOR_INVITE_PENDING_ROLE,
    OPERATOR_INVOLVEMENT_ROLES,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.entities.story_arc import StoryArcBeat
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
    sanitize_messages_for_tolerance,
)
from kokoro_link.domain.value_objects.timezone import timezone_for_id
from kokoro_link.infrastructure.prompt.initial_relationship import (
    render_initial_relationship_seed_lines,
)

if TYPE_CHECKING:
    # StoryArcService imports nothing from this module — the TYPE_CHECKING
    # guard exists only to avoid a runtime cycle if either side later
    # grows additional cross-imports. The actual injection is duck-typed.
    from kokoro_link.application.services.story_arc_service import StoryArcService

_LOGGER = logging.getLogger(__name__)

_DEFAULT_UPCOMING_LIMIT = 3
# How far back we still consider an activity "just finished" — the point
# of surfacing it is to anchor a gap-filler transition line. A 3-hour cap
# keeps a morning meeting from resurfacing in an evening proactive push.
_JUST_FINISHED_WINDOW = timedelta(hours=3)


_DIALOGUE_CONTEXT_LIMIT = 40

# How many days ahead the rolling-window pre-planner covers. 3 days
# (today + tomorrow + day-after) is the sweet spot: it's enough for the
# common "明天/後天有什麼計畫" conversation without burning N× planner
# cost; anything further out is intentionally left vague so the model
# admits "還沒安排到那麼遠" instead of fabricating commitments that
# won't match when the actual day comes.
_ROLLING_WINDOW_DAYS = 3


class ScheduleService:
    def __init__(
        self,
        *,
        repository: ScheduleRepositoryPort,
        planner: SchedulePlannerPort,
        local_tz: tzinfo,
        conversation_repository: ConversationRepositoryPort | None = None,
        dialogue_summarizer: DialogueSummarizerPort | None = None,
        story_arc_service: "StoryArcService | None" = None,
        calendar_context_port: CalendarContextPort | None = None,
        weather_context_port: WeatherContextPort | None = None,
        behavioral_pattern_repository: BehavioralPatternRepositoryPort | None = None,
        relationship_seed_repository: (
            CharacterOperatorRelationshipSeedRepositoryPort | None
        ) = None,
        operator_persona_service=None,  # noqa: ANN001 — optional app service
        operator_profile_service=None,  # noqa: ANN001 — optional, resolves primary_language
    ) -> None:
        self._repository = repository
        self._planner = planner
        self._local_tz = local_tz
        self._conversation_repository = conversation_repository
        self._dialogue_summarizer = dialogue_summarizer
        # FRONTEND_I18N_PLAN — planner output (activity descriptions,
        # locations) is user-visible, so it follows the same operator
        # language fact as chat / proactive / feed.
        self._operator_profile_service = operator_profile_service
        # Optional calendar provider. When wired, every plan_day call
        # receives a natural-language block describing today's real-
        # world civil calendar (weekday, holiday name, 連假 position,
        # nearby holidays, season) so the LLM planner can adapt the
        # schedule to a 上班族's blue Monday, a 學生's 國慶連假, etc.
        # When unwired the planner falls back to the legacy "weekday
        # name only" path — the schedule still generates fine, just
        # without holiday awareness.
        self._calendar_context_port = calendar_context_port
        # Real-world weather fact layer — same fall-through shape as
        # calendar but async (HTTP-backed Open-Meteo adapter). Planner
        # uses it so "下雨改室內" arrangements appear naturally without
        # any hardcoded if-rain branches.
        self._weather_context_port = weather_context_port
        self._relationship_seed_repository = relationship_seed_repository
        self._operator_persona_service = operator_persona_service
        # Optional arc service. When wired, every plan_day call enriches
        # the planner prompt with today's scheduled beat (location, NPCs,
        # dramatic question) so the day's activities embed the scene
        # rather than running parallel to it. ``auto_start=False`` on
        # ensure_active_arc means we never trigger arc planning from
        # the schedule path — that's chat / proactive's job.
        self._story_arc_service = story_arc_service
        # Optional behavioural pattern repo (HUMANIZATION_ROADMAP §3.3).
        # When wired, ``plan_day`` is told about statistically observed
        # weekly recurrences so the LLM can decide whether to continue
        # them. Absent = legacy path, planner sees only today's facts.
        self._behavioral_patterns = behavioral_pattern_repository
        # Single-flight locks per (character_id, date) so concurrent
        # callers don't each fire ``plan_day`` when the row is still
        # missing. Common race: ``/schedule/current`` poll from the
        # chat panel fires the same moment ``ChatService.send_message``
        # does — without this lock the LLM planner ran twice back to
        # back. Second caller waits, then hits the short-circuit.
        self._plan_locks: dict[tuple[str, date], asyncio.Lock] = {}

    def set_behavioral_pattern_repository(
        self, repository: BehavioralPatternRepositoryPort | None,
    ) -> None:
        """Late-bind the behavioural-pattern repo (HUMANIZATION_ROADMAP §3.3).

        ``ScheduleService`` is constructed in :mod:`bootstrap.container`
        before the observability engine builds its repositories, so the
        repo is injected via this setter once available. Absent =
        legacy path (planner sees no recurring patterns)."""
        self._behavioral_patterns = repository

    @property
    def local_tz(self) -> tzinfo:
        return self._local_tz

    def describe_calendar(
        self,
        target: date | None = None,
        *,
        operator: OperatorProfile | None = None,
    ) -> str:
        """Public accessor for the calendar block on ``target`` date.

        Chat and proactive paths use this so the prompt builder sees
        the same calendar context the schedule planner saw — keeping
        the planner's "today is 春節" framing and the model's reply
        framing in sync without each path wiring the port separately.
        """
        return self._describe_calendar(target or self.today(), operator=operator)

    async def describe_weather(
        self,
        target: date | None = None,
        *,
        operator: OperatorProfile | None = None,
    ) -> str:
        """Public async accessor for the current-weather block.

        Same façade pattern as :meth:`describe_calendar` — chat /
        proactive / feed paths read the weather string through the
        schedule service so every prompt site quotes the same fact,
        and we don't have to wire :class:`WeatherContextPort` into
        every service individually.
        """
        return await self._describe_weather(target or self.today(), operator=operator)

    def today(self, now: datetime | None = None) -> date:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(self._local_tz).date()

    async def today_for_character(
        self,
        character: Character,
        now: datetime | None = None,
    ) -> date:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        local_tz = await self._resolve_operator_timezone(character)
        return moment.astimezone(local_tz).date()

    async def timezone_for_character(self, character: Character) -> tzinfo:
        return await self._resolve_operator_timezone(character)

    async def ensure_schedule(
        self,
        character: Character,
        *,
        date_: date | None = None,
        now: datetime | None = None,
    ) -> DailySchedule:
        local_tz = await self._resolve_operator_timezone(character)
        local_today = await self.today_for_character(character, now=now)
        target = date_ or local_today
        # Fast path: schedule already exists with activities AND has
        # been fully planned by the LLM → no need to acquire the
        # plan-lock just to read. A row with activities but
        # ``is_planned=False`` (a chat-extracted future-commitment
        # seed) still needs the planner to fold a full day around it,
        # so we fall through to the slow path below.
        #
        # One extra short-circuit guard: a fully-planned *today* row that
        # was actually generated on an earlier local day is a stale
        # forecast (its weather is another day's weather). When the day it
        # covers finally arrives we re-plan it with that day's real
        # weather instead of freezing "上週在下雨" into a sunny day — see
        # ``_is_stale_current_day_plan``.
        existing = await self._repository.get(character.id, target)
        if (
            existing is not None
            and existing.activities
            and existing.is_planned
            and not self._is_stale_current_day_plan(
                existing, target=target, local_today=local_today, local_tz=local_tz,
            )
        ):
            return existing
        # Slow path: row missing, empty, seed-only, or a stale current-day
        # forecast. Serialise concurrent callers on a per-(character, date)
        # lock so only one plan_day fires per day even when chat send +
        # schedule-panel poll race.
        lock = self._plan_locks.setdefault((character.id, target), asyncio.Lock())
        async with lock:
            # Re-check under the lock — another caller may have just
            # populated the schedule while we were waiting.
            existing = await self._repository.get(character.id, target)
            if (
                existing is not None
                and existing.activities
                and existing.is_planned
                and not self._is_stale_current_day_plan(
                    existing, target=target, local_today=local_today,
                    local_tz=local_tz,
                )
            ):
                return existing
            # An existing-but-empty schedule is almost always the residue
            # of a past planner failure (pre-fix: we used to persist empty
            # on exception). Treat it as "please retry" rather than "this
            # day is blank" so stuck characters self-heal on the next turn.
            # ``pre_commitments`` carries activities that must survive the
            # plan_day call — chat-extracted seeds (e.g. "明天 7 點看電影")
            # and, on a stale-today re-plan, any promise to the operator —
            # so the planner is told to build the day *around* them, not
            # overwrite them.
            pre_commitments = self._carry_forward_commitments(existing)
            summary = await self._summarize_recent_dialogue(character)
            today_beat, upcoming = await self._collect_arc_beats(
                character=character, target=target,
            )
            operator = await self._resolve_operator_profile(character)
            calendar_context = self._describe_calendar(target, operator=operator)
            weather_context = await self._weather_for_plan(
                target, operator=operator, local_today=local_today,
            )
            recurring_patterns = await self._load_recurring_patterns(
                character.id,
            )
            relationship_seed = await self._load_relationship_seed(character)
            operator_persona_lines = await self._load_operator_persona_lines(
                character,
            )
            try:
                schedule = await self._planner.plan_day(
                    character=character,
                    date_=target,
                    local_tz=local_tz,
                    recent_dialogue_summary=summary,
                    today_beat=today_beat,
                    upcoming_beats=upcoming,
                    calendar_context=calendar_context,
                    weather_context=weather_context,
                    operator_relationship_context=(
                        "\n".join(render_initial_relationship_seed_lines(
                            relationship_seed,
                        ))
                    ),
                    operator_persona_lines=tuple(operator_persona_lines),
                    schedule_involvement_policy=(
                        relationship_seed.schedule_involvement_policy
                        if relationship_seed is not None else "none"
                    ),
                    pre_committed_activities=pre_commitments,
                    recurring_patterns=recurring_patterns,
                    operator_primary_language=_operator_language(operator),
                )
            except Exception:
                # Don't persist on failure — if we saved an empty schedule,
                # ``ensure_schedule`` would short-circuit on the next call
                # and the character would be "activity-less" for the rest
                # of the day. Return an ephemeral empty so the current
                # prompt still renders, and let the next turn retry.
                _LOGGER.exception(
                    "Schedule planning failed for character=%s; "
                    "returning ephemeral empty and will retry next turn",
                    character.id,
                )
                return DailySchedule.create(
                    character_id=character.id,
                    date_=target,
                    activities=[],
                )
            await self._repository.save(schedule)
            return schedule

    async def ensure_window(
        self,
        character: Character,
        *,
        start: date | None = None,
        now: datetime | None = None,
        days: int = _ROLLING_WINDOW_DAYS,
    ) -> list[DailySchedule]:
        """Pre-plan a rolling window of ``days`` schedules from ``start``.

        Drives the "answer questions about tomorrow / day-after"
        capability: the proactive tick calls this so the model in chat
        can reference what the character plans to be doing on those
        days, instead of inventing commitments that won't match when
        the actual day arrives.

        Each day is generated via :meth:`ensure_schedule` so the
        per-(char, date) lock and short-circuit semantics apply. A
        single failing day is logged and skipped — partial coverage is
        still strictly better than nothing.

        ``days`` is clamped to ``[1, 7]`` so an operator can't accidentally
        request a 365-day pre-plan and burn LLM credits; 7 days is
        already wider than the "next week" question can sensibly ask
        about and the prompt-side caller only ever shows the first three.
        """
        anchor = start or await self.today_for_character(character, now=now)
        span = max(1, min(7, days))
        out: list[DailySchedule] = []
        for offset in range(span):
            target = anchor + timedelta(days=offset)
            try:
                schedule = await self.ensure_schedule(
                    character, date_=target, now=now,
                )
            except Exception:
                _LOGGER.exception(
                    "ensure_window: ensure_schedule failed character=%s date=%s",
                    character.id, target,
                )
                continue
            out.append(schedule)
        return out

    async def load_upcoming_schedules(
        self,
        character_id: str,
        *,
        start_after: date,
        days: int = _ROLLING_WINDOW_DAYS - 1,
    ) -> list[DailySchedule]:
        """Return the next ``days`` schedules *strictly after* ``start_after``.

        Used by chat / proactive prompt builders to surface the
        upcoming-day context (tomorrow + day-after). Days that haven't
        been pre-planned yet are omitted — we return what's already in
        the repository rather than triggering planner calls on the hot
        path. ``ensure_window`` (run from the proactive tick) is the
        eager generator; this read-only call only renders what's there.
        """
        if days <= 0:
            return []
        results: list[DailySchedule] = []
        for offset in range(1, days + 1):
            target = start_after + timedelta(days=offset)
            schedule = await self._repository.get(character_id, target)
            if schedule is None or not schedule.activities:
                continue
            results.append(schedule)
        return results

    async def regenerate(
        self,
        character: Character,
        *,
        date_: date | None = None,
    ) -> DailySchedule:
        local_tz = await self._resolve_operator_timezone(character)
        local_today = await self.today_for_character(character)
        target = date_ or local_today
        summary = await self._summarize_recent_dialogue(character)
        today_beat, upcoming = await self._collect_arc_beats(
            character=character, target=target,
        )
        operator = await self._resolve_operator_profile(character)
        calendar_context = self._describe_calendar(target, operator=operator)
        weather_context = await self._weather_for_plan(
            target, operator=operator, local_today=local_today,
        )
        # Carry forward any chat-extracted seed commitments on the
        # existing row so a manual regenerate doesn't silently drop
        # them. Memorialised activities (the past) we always preserve.
        existing = await self._repository.get(character.id, target)
        pre_commitments: tuple[ScheduleActivity, ...] = ()
        if existing is not None and not existing.is_planned:
            pre_commitments = tuple(existing.activities)
        recurring_patterns = await self._load_recurring_patterns(character.id)
        relationship_seed = await self._load_relationship_seed(character)
        operator_persona_lines = await self._load_operator_persona_lines(
            character,
        )
        schedule = await self._planner.plan_day(
            character=character,
            date_=target,
            local_tz=local_tz,
            recent_dialogue_summary=summary,
            today_beat=today_beat,
            upcoming_beats=upcoming,
            calendar_context=calendar_context,
            weather_context=weather_context,
            operator_relationship_context="\n".join(
                render_initial_relationship_seed_lines(relationship_seed),
            ),
            operator_persona_lines=tuple(operator_persona_lines),
            schedule_involvement_policy=(
                relationship_seed.schedule_involvement_policy
                if relationship_seed is not None else "none"
            ),
            pre_committed_activities=pre_commitments,
            recurring_patterns=recurring_patterns,
            operator_primary_language=_operator_language(operator),
        )
        await self._repository.save(schedule)
        return schedule

    async def _resolve_operator_language(self, character) -> str:  # noqa: ANN001
        """Resolve character owner's pinned ``primary_language``; falls
        back to ``"zh-TW"`` when the service is unwired or the lookup
        fails (matches ``ProactiveDispatcher._load_operator_language``)."""
        default = "zh-TW"
        service = self._operator_profile_service
        if service is None:
            return default
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        lang = getattr(operator, "primary_language", "") or ""
        return lang.strip() or default

    async def _resolve_operator_profile(self, character) -> OperatorProfile | None:  # noqa: ANN001
        service = self._operator_profile_service
        if service is None:
            return None
        user_id = getattr(character, "user_id", None) or "default"
        try:
            return await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return None

    async def _resolve_operator_timezone(self, character) -> tzinfo:  # noqa: ANN001
        service = self._operator_profile_service
        if service is None:
            return self._local_tz
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await service.get_for_user(user_id)
            return timezone_for_id(getattr(operator, "timezone_id", None))
        except Exception:  # pragma: no cover - defensive
            return self._local_tz

    async def _collect_arc_beats(
        self,
        *,
        character: Character,
        target: date,
    ) -> tuple[StoryArcBeat | None, tuple[StoryArcBeat, ...]]:
        """Pull today's beat + next 1–2 upcoming beats from the active arc.

        Returns ``(None, ())`` when no arc service is wired, no active
        arc exists, or any failure occurs — schedule generation continues
        unconditionally; arc context is enrichment, not a hard dep.

        ``auto_start=False`` is critical: the schedule path must never
        trigger arc planning. First-arc lazy creation can happen in chat,
        but completed-arc season opening belongs to proactive/background
        work. Triggering it from a schedule regenerate could fire an LLM
        call from a UI button the operator didn't expect.
        """
        if self._story_arc_service is None:
            return None, ()
        try:
            arc = await self._story_arc_service.ensure_active_arc(
                character, today=target, auto_start=False,
            )
        except Exception:
            _LOGGER.exception(
                "schedule arc lookup failed character=%s", character.id,
            )
            return None, ()
        if arc is None:
            return None, ()
        try:
            today_beats = arc.beats_on(target)
            today_beat = today_beats[0] if today_beats else None
            upcoming = tuple(
                arc.forward_beats(after=target, limit=2, include_today=False)
            )
        except Exception:
            _LOGGER.exception(
                "schedule arc beat slicing failed character=%s arc=%s",
                character.id, arc.id,
            )
            return None, ()
        # Fallback: today is a gap day (no beat scheduled for it, e.g. the
        # planner left empty days between beats, or a beat scheduled for
        # today was mark_realized in an earlier turn). Promote the next
        # forward beat into the today_beat slot so the planner still gets
        # an arc anchor — the planner detects ``scheduled_date != target``
        # and renders it as a "preparation/anticipation" block instead of
        # a "today's scene" directive, so this isn't lying about timing.
        if today_beat is None and upcoming:
            today_beat = upcoming[0]
            upcoming = upcoming[1:]
        return today_beat, upcoming

    def _describe_calendar(
        self,
        target: date,
        *,
        operator: OperatorProfile | None = None,
    ) -> str:
        """Render the natural-language calendar block for ``target``.

        Failures (an unknown region inside the holidays package, an
        adapter raising at runtime) degrade to an empty string — the
        planner then loses holiday awareness for this turn but the
        schedule still generates. Logged for observability.
        """
        if self._calendar_context_port is None:
            return ""
        try:
            return self._calendar_context_port.describe(
                target,
                region=calendar_region_from_operator(operator),
            )
        except Exception:
            _LOGGER.exception(
                "calendar context describe failed for target=%s", target,
            )
            return ""

    async def _describe_weather(
        self,
        target: date,
        *,
        operator: OperatorProfile | None = None,
    ) -> str:
        """Async counterpart for weather. ``target`` is unused by the
        current adapter (Open-Meteo always reports "今天 + 此刻")
        but kept in the signature so future per-date forecasts plug in
        without a service-level signature change."""
        if self._weather_context_port is None:
            return ""
        try:
            return await self._weather_context_port.describe(
                location=weather_location_from_operator(operator),
            )
        except Exception:
            _LOGGER.exception(
                "weather context describe failed for target=%s", target,
            )
            return ""

    async def _weather_for_plan(
        self,
        target: date,
        *,
        operator: OperatorProfile | None,
        local_today: date,
    ) -> str:
        """Weather block for *planning* ``target``.

        The Open-Meteo adapter only reports the current day, so a plan for
        any other day must stay weather-agnostic rather than freeze
        today's conditions into a future day — that mismatch is the seed
        behind "放晴後角色還在說下雨". The current local day still gets a
        real weather fact; future pre-plans become weather-aware only once
        the day arrives and is re-planned (see
        :meth:`_is_stale_current_day_plan`).
        """
        if target != local_today:
            return ""
        return await self._describe_weather(target, operator=operator)

    def _is_stale_current_day_plan(
        self,
        existing: DailySchedule,
        *,
        target: date,
        local_today: date,
        local_tz: tzinfo,
    ) -> bool:
        """``True`` when a fully-planned schedule for *today* was actually
        generated on an earlier local day.

        Such a row was planned from a forecast (and, with the same-day
        weather adapter, literally from a different day's weather), so when
        the day it covers finally arrives we re-plan it once with that
        day's real weather. We only do this while it is safe — no activity
        has been memorialised yet — so a same-day refresh can never
        duplicate memories the morning already recorded.
        """
        if target != local_today:
            return False
        if any(activity.memorialized for activity in existing.activities):
            return False
        generated_local = self._local_date_of(existing.generated_at, local_tz)
        return generated_local < local_today

    @staticmethod
    def _local_date_of(moment: datetime, local_tz: tzinfo) -> date:
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        return moment.astimezone(local_tz).date()

    @staticmethod
    def _carry_forward_commitments(
        existing: DailySchedule | None,
    ) -> tuple[ScheduleActivity, ...]:
        """Activities that must survive a (re)plan of the same day.

        - A seed-only row (a chat-extracted future commitment not yet
          folded into a full day) carries *all* its activities.
        - A stale current-day row being re-planned carries the activities
          that represent a promise to the operator, so a same-day weather
          refresh never silently drops "明天 7 點看電影" or a pending
          invite. Memorialised history is excluded by construction — the
          re-plan only runs while nothing has been memorialised.
        """
        if existing is None or not existing.activities:
            return ()
        if not existing.is_planned:
            return tuple(existing.activities)
        return tuple(
            activity
            for activity in existing.activities
            if _has_operator_commitment(activity)
        )

    async def _load_recurring_patterns(
        self, character_id: str,
    ) -> tuple[BehavioralPattern, ...]:
        """HUMANIZATION_ROADMAP §3.3 — fetch the strongest schedule-shaped
        patterns for planner injection.

        We filter to ``recurring_activity`` + ``time_preference`` here so
        ``phrase_habit`` rows (which are about how the character talks)
        do not leak into a schedule-only prompt. Returns an empty tuple
        when no repo is wired or the lookup fails — the planner stays on
        the legacy "no patterns" path with no behavioural change.
        """
        if self._behavioral_patterns is None:
            return ()
        try:
            patterns = await self._behavioral_patterns.list_for_character(
                character_id,
                kinds=(KIND_RECURRING_ACTIVITY, KIND_TIME_PREFERENCE),
                limit=8,
            )
        except Exception:
            _LOGGER.exception(
                "behavioral_pattern: list_for_character failed character=%s",
                character_id,
            )
            return ()
        return tuple(patterns)

    async def _load_relationship_seed(
        self,
        character: Character,
    ) -> CharacterOperatorRelationshipSeed | None:
        if self._relationship_seed_repository is None:
            return None
        operator_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            return await self._relationship_seed_repository.get(
                character_id=character.id,
                operator_id=operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "schedule relationship seed lookup failed character=%s",
                character.id,
            )
            return None

    async def _load_operator_persona_lines(self, character: Character) -> list[str]:
        service = self._operator_persona_service
        if service is None:
            return []
        operator_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            persona = await service.get_current(character.id, operator_id)
            return list(service.render_for_prompt(persona))
        except Exception:
            _LOGGER.exception(
                "schedule operator persona render failed character=%s",
                character.id,
            )
            return []

    async def _summarize_recent_dialogue(self, character: Character) -> str:
        """Pull dialogue merged across every source, filter tool-only
        turns, and ask the summarizer to condense it.

        Cross-source merge: the character is one person on web /
        telegram / line, so the planner sees a unified timeline rather
        than only the web silo. Empty string when any link in the chain
        is unwired or returns nothing — the planner treats that as "no
        dialogue context" and skips the prompt section."""
        if (
            self._conversation_repository is None
            or self._dialogue_summarizer is None
        ):
            return ""
        try:
            messages = await self._conversation_repository.recent_messages_for_character(
                character.id,
                limit=_DIALOGUE_CONTEXT_LIMIT,
                exclude_tool_only=True,
            )
        except Exception:
            _LOGGER.exception(
                "schedule dialogue load failed character=%s", character.id,
            )
            return ""
        if not messages:
            return ""
        messages = sanitize_messages_for_tolerance(
            messages,
            content_tolerance=CONTENT_TOLERANCE_FRONTIER,
        )
        if not messages:
            return ""
        try:
            return await self._dialogue_summarizer.summarize(
                character=character, messages=messages,
            )
        except Exception:
            _LOGGER.exception(
                "schedule dialogue summarise failed character=%s", character.id,
            )
            return ""

    async def get_schedule(
        self,
        character_id: str,
        *,
        date_: date | None = None,
    ) -> DailySchedule | None:
        return await self._repository.get(character_id, date_ or self.today())

    async def get_schedule_response(
        self,
        character_id: str,
        *,
        date_: date | None = None,
    ) -> DailyScheduleResponse | None:
        schedule = await self.get_schedule(character_id, date_=date_)
        if schedule is None:
            return None
        return DailyScheduleResponse.from_domain(schedule)

    def resolve_current(
        self,
        schedule: DailySchedule,
        *,
        now: datetime | None = None,
        upcoming_limit: int = _DEFAULT_UPCOMING_LIMIT,
    ) -> tuple[
        ScheduleActivity | None,
        list[ScheduleActivity],
        ScheduleActivity | None,
    ]:
        """Return ``(current, upcoming, just_finished)`` for ``now``.

        ``just_finished`` is populated only when ``current is None`` — in
        a gap we want to anchor the model with what wrapped up just
        before. When the character is actively doing something the
        current activity is already in prompt, so we suppress the
        just-finished signal to avoid prompt clutter.
        """
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        current = schedule.activity_at(moment)
        upcoming = schedule.upcoming(moment)[:upcoming_limit]
        just_finished: ScheduleActivity | None = None
        if current is None:
            just_finished = schedule.most_recent_past(
                moment, within=_JUST_FINISHED_WINDOW,
            )
        return current, upcoming, just_finished

    def resolve_completed_today(
        self,
        schedule: DailySchedule,
        *,
        now: datetime | None = None,
        local_tz: tzinfo | None = None,
        limit: int = 8,
    ) -> list[ScheduleActivity]:
        """Return recently completed non-encounter activities for today's prompt.

        This is a deterministic same-day timeline, not long-term memory recall.
        It intentionally excludes encounter-owned blocks because those are
        memorialized by the encounter runner.
        """
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        tz = local_tz or self._local_tz
        today_local = moment.astimezone(tz).date()
        completed = [
            activity
            for activity in schedule.activities
            if activity.end_at < moment
            and activity.end_at.astimezone(tz).date() == today_local
            and not _is_encounter_activity(activity)
        ]
        completed.sort(key=lambda activity: activity.end_at)
        if limit <= 0:
            return []
        return completed[-limit:]

    def resolve_pending_invites(
        self,
        schedule: DailySchedule,
        *,
        now: datetime | None = None,
        limit: int = 1,
    ) -> list[ScheduleActivity]:
        """Return upcoming activities where the operator has not agreed yet."""
        return self.resolve_pending_invites_from_schedules(
            [schedule],
            now=now,
            limit=limit,
        )

    def resolve_pending_invites_from_schedules(
        self,
        schedules: list[DailySchedule] | tuple[DailySchedule, ...],
        *,
        now: datetime | None = None,
        limit: int = 1,
    ) -> list[ScheduleActivity]:
        """Return pending operator invites across a planned schedule window."""
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        if limit <= 0:
            return []
        pending = [
            activity
            for schedule in schedules
            for activity in schedule.activities
            if activity.end_at > moment
            and any(
                ref.actor_kind == "operator"
                and ref.role == OPERATOR_INVITE_PENDING_ROLE
                for ref in activity.participant_refs
            )
        ]
        pending.sort(key=lambda activity: activity.start_at)
        return pending[:limit]

    async def current_activity_response(
        self,
        character_id: str,
        *,
        now: datetime | None = None,
        character: Character | None = None,
    ) -> CurrentActivityResponse:
        moment = now or datetime.now(timezone.utc)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        local_tz = (
            await self._resolve_operator_timezone(character)
            if character is not None
            else self._local_tz
        )
        target_date = moment.astimezone(local_tz).date()
        schedule = await self._repository.get(character_id, target_date)
        if schedule is None:
            return CurrentActivityResponse.build(now=moment, current=None, upcoming=[])
        current, upcoming, _ = self.resolve_current(schedule, now=moment)
        return CurrentActivityResponse.build(
            now=moment, current=current, upcoming=upcoming,
        )

    async def delete_for_character(self, character_id: str) -> int:
        return await self._repository.delete_for_character(character_id)

    async def apply_adjustments(
        self,
        *,
        character_id: str,
        adjustments: list[ScheduleAdjustment],
        date_: date | None = None,
        character: Character | None = None,
    ) -> DailySchedule | None:
        """Apply post-turn schedule mutations and persist the result.

        Adjustments are bucketed by their effective date (
        ``target_date_iso`` when set, otherwise ``date_`` arg, otherwise
        today). The "today" bucket — if any — drives the return value
        for backward compatibility; future-date buckets are processed
        as a side effect and surfaced through ``ensure_schedule`` later.

        For future dates the behaviour is **lazy-create + seed**: if no
        schedule row exists yet for that date, one is created with
        ``is_planned=False`` containing only the chat-extracted
        commitments. The next ``ensure_schedule`` pass (background tick
        or on-demand) reads those seeds and asks the LLM planner to
        weave a full day around them. ``remove`` / ``modify`` against
        a non-existent future row are dropped silently — there's
        nothing to remove or modify yet.

        Rules:

        - ``add`` requires start/end/description/category — creates a
          new ``ScheduleActivity`` in the character's local timezone.
        - ``remove`` drops the matching activity.
        - ``modify`` replaces fields that are non-``None``; fields left
          as ``None`` stay as-is. Already-memorialised activities are
          **not** modified — we preserve the recorded history.
        - New / modified blocks that overlap existing ones trim the
          latter's start forward so ``activity_at`` stays unambiguous.
        """
        if not adjustments:
            return None
        local_tz = (
            await self._resolve_operator_timezone(character)
            if character is not None
            else self._local_tz
        )
        default_target = date_ or (
            await self.today_for_character(character)
            if character is not None
            else self.today()
        )
        today = (
            await self.today_for_character(character)
            if character is not None
            else self.today()
        )
        buckets: dict[date, list[ScheduleAdjustment]] = {}
        for adj in adjustments:
            parsed = _parse_iso_date(adj.target_date_iso) or default_target
            buckets.setdefault(parsed, []).append(adj)

        today_result: DailySchedule | None = None
        for target, group in buckets.items():
            updated = await self._apply_adjustments_for_date(
                character_id=character_id,
                adjustments=group,
                target=target,
                today=today,
                local_tz=local_tz,
            )
            if target == default_target and updated is not None:
                today_result = updated
        return today_result

    async def _apply_adjustments_for_date(
        self,
        *,
        character_id: str,
        adjustments: list[ScheduleAdjustment],
        target: date,
        today: date,
        local_tz: tzinfo,
    ) -> DailySchedule | None:
        """Apply a single-date bucket of adjustments.

        Same business rules as the public :meth:`apply_adjustments` but
        scoped to one civil date. Returns the persisted schedule for
        the bucket, or ``None`` when nothing changed (or there was no
        row to mutate and no seed activities to create).
        """
        existing = await self._repository.get(character_id, target)
        is_future_seed = (
            existing is None and target > today
            and any(a.action == "add" for a in adjustments)
        )
        if existing is None and not is_future_seed:
            # No row + no add for a future date → nothing to do.
            # remove / modify against a non-existent row are dropped.
            return None
        if existing is None:
            # Lazy-create future-date seed schedule. is_planned=False so
            # the next ensure_schedule call expands it via plan_day.
            schedule = DailySchedule.create(
                character_id=character_id,
                date_=target,
                activities=[],
                is_planned=False,
            )
        else:
            schedule = existing

        current_activities: list[ScheduleActivity] = list(schedule.activities)
        mutated = False

        for adj in adjustments:
            action = adj.action
            if action == "remove":
                if adj.activity_id is None:
                    continue
                before = len(current_activities)
                current_activities = [
                    a for a in current_activities
                    if a.id != adj.activity_id or a.memorialized
                ]
                if len(current_activities) != before:
                    mutated = True
                continue

            if action == "modify":
                if adj.activity_id is None:
                    continue
                updated_acts, changed = _apply_modify(
                    current_activities,
                    activity_id=adj.activity_id,
                    adj=adj,
                    date_=target,
                    local_tz=local_tz,
                )
                if changed:
                    current_activities = updated_acts
                    mutated = True
                continue

            if action == "add":
                new_activity = _build_added_activity(
                    adj, date_=target, local_tz=local_tz,
                )
                if new_activity is None:
                    continue
                current_activities.append(new_activity)
                mutated = True

        if not mutated:
            return None

        current_activities = _resolve_overlaps(current_activities)
        updated_schedule = schedule.with_activities(current_activities)
        await self._repository.save(updated_schedule)
        return updated_schedule


def _apply_modify(
    activities: list[ScheduleActivity],
    *,
    activity_id: str,
    adj: ScheduleAdjustment,
    date_: date,
    local_tz: tzinfo,
) -> tuple[list[ScheduleActivity], bool]:
    out: list[ScheduleActivity] = []
    changed = False
    for activity in activities:
        if activity.id != activity_id:
            out.append(activity)
            continue
        if activity.memorialized:
            # Past activities have already become memories — do not
            # rewrite the history the character "remembers".
            out.append(activity)
            continue
        new_start = (
            _combine_local(date_, adj.start, local_tz)
            if adj.start is not None
            else activity.start_at
        )
        new_end = (
            _combine_local(date_, adj.end, local_tz)
            if adj.end is not None
            else activity.end_at
        )
        if new_start is None or new_end is None or new_end <= new_start:
            out.append(activity)
            continue
        new_description = adj.description or activity.description
        new_category = adj.category or activity.category
        new_location = adj.location if adj.location is not None else activity.location
        new_busy = adj.busy_score if adj.busy_score is not None else activity.busy_score
        participant_refs = _with_operator_involvement(
            activity.participant_refs,
            role=adj.operator_involvement,
            display_name=adj.operator_display_name,
        )
        out.append(
            replace(
                activity,
                start_at=new_start,
                end_at=new_end,
                description=new_description,
                category=new_category,
                location=new_location,
                busy_score=max(0.0, min(1.0, new_busy)),
                participant_refs=participant_refs,
            )
        )
        changed = True
    return out, changed


def _build_added_activity(
    adj: ScheduleAdjustment,
    *,
    date_: date,
    local_tz: tzinfo,
) -> ScheduleActivity | None:
    start = _combine_local(date_, adj.start, local_tz)
    end = _combine_local(date_, adj.end, local_tz)
    if start is None or end is None:
        return None
    if end <= start:
        return None
    if not adj.description or not adj.category:
        return None
    try:
        return ScheduleActivity.create(
            start_at=start,
            end_at=end,
            description=adj.description,
            category=adj.category,
            location=adj.location,
            busy_score=adj.busy_score,
            participant_refs=_with_operator_involvement(
                (),
                role=adj.operator_involvement,
                display_name=adj.operator_display_name,
            ),
        )
    except ValueError:
        return None


def _combine_local(date_: date, raw: str | None, local_tz: tzinfo) -> datetime | None:
    if raw is None:
        return None
    parts = raw.split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if hour == 24 and minute == 0:
        return datetime.combine(date_, time(0, 0), tzinfo=local_tz) + timedelta(days=1)
    if not (0 <= hour <= 23) or not (0 <= minute <= 59):
        return None
    return datetime.combine(date_, time(hour, minute), tzinfo=local_tz)


def _is_encounter_activity(activity: ScheduleActivity) -> bool:
    return any(ref.role == "encounter_partner" for ref in activity.participant_refs)


def _has_operator_commitment(activity: ScheduleActivity) -> bool:
    """``True`` when the activity encodes a promise to / plan with the
    operator (a pending invite, a wish, or a confirmed shared plan).

    Used to protect such commitments when a stale current-day schedule is
    re-planned for a weather refresh — we never want a same-day refresh to
    silently drop "明天 7 點看電影" or an unanswered invite.
    """
    return any(
        ref.role in OPERATOR_INVOLVEMENT_ROLES
        for ref in activity.participant_refs
    )


def _with_operator_involvement(
    existing: tuple[ParticipantRef, ...],
    *,
    role: str | None,
    display_name: str | None,
) -> tuple[ParticipantRef, ...]:
    if role is None:
        return existing
    if role not in OPERATOR_INVOLVEMENT_ROLES:
        return existing
    kept = tuple(
        ref
        for ref in existing
        if not (ref.actor_kind == "operator" and ref.role in OPERATOR_INVOLVEMENT_ROLES)
    )
    return kept + (
        ParticipantRef(
            actor_kind="operator",
            actor_id=None,
            display_name=(display_name or "使用者").strip() or "使用者",
            role=role,
        ),
    )


def _parse_iso_date(raw: str | None) -> date | None:
    """Parse a ``YYYY-MM-DD`` date string with full tolerance.

    Returns ``None`` for any malformed / out-of-range input — apply_
    adjustments treats that as "fall back to the default bucket"
    (today / passed-in ``date_``) instead of raising.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _operator_language(operator: OperatorProfile | None) -> str:
    if operator is None:
        return "zh-TW"
    lang = (operator.primary_language or "").strip()
    return lang or "zh-TW"


def _resolve_overlaps(activities: list[ScheduleActivity]) -> list[ScheduleActivity]:
    """Sort and trim overlaps so ``activity_at`` remains deterministic.

    Memorialized activities are considered immutable anchors — if a new
    block overlaps one, the new block gets its start pushed back instead.
    """
    ordered = sorted(activities, key=lambda a: a.start_at)
    out: list[ScheduleActivity] = []
    for activity in ordered:
        if out:
            last = out[-1]
            if activity.start_at < last.end_at:
                # overlap — push new one's start to last's end
                pushed_start = last.end_at
                if activity.end_at <= pushed_start:
                    continue  # fully swallowed
                if activity.memorialized:
                    # we shouldn't have gotten here for completed blocks,
                    # but if we do, preserve them and trim the previous.
                    out[-1] = replace(last, end_at=activity.start_at)
                    out.append(activity)
                    continue
                activity = replace(activity, start_at=pushed_start)
        out.append(activity)
    return out
