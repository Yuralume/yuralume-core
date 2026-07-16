"""Cheap heuristic proactive gate.

Signals (all short-circuit, in order):

1. Character state has ``last_active_at`` — user must have been idle
   for at least ``_MIN_IDLE_MINUTES`` so we don't interrupt an ongoing
   conversation.
2. Per-day rate limit (``character.proactive_daily_limit``) respected.
3. Per-attempt cooldown (``character.proactive_cooldown_minutes``)
   since the last logged attempt of any outcome — keeps the dispatcher
   from burning tokens evaluating every tick.
4. **Night-hours floor** — regardless of schedule, don't fire between
   ``_QUIET_HOUR_START`` and ``_QUIET_HOUR_END`` in the character's
   local time. This is a hard safety net so a server restart at 00:53
   can't send a "早安" message in the middle of the night. It's a
   belt-and-braces defence — the schedule-aware check below also
   catches this when a daily schedule has been generated, but that
   requires ``schedule_resolver`` wiring which the proactive path
   historically skipped.
5. Schedule-aware quiet period: if the character's current activity
   category reads like sleep / rest (or energy is very low), skip.

All of this is pure Python and returns in microseconds, so it's safe
to run on every tick.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone, tzinfo

from kokoro_link.contracts.proactive import GateVerdict, ProactiveGatePort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger

_MIN_IDLE_MINUTES = 10.0
_QUIET_CATEGORY_TOKENS: frozenset[str] = frozenset({
    "sleep", "asleep", "rest", "resting", "nap", "napping",
    "睡", "睡眠", "休息", "安眠", "就寢",
})
_LOW_ENERGY_THRESHOLD = 15
_QUIET_HOUR_START = 0   # inclusive — 00:00 local
_QUIET_HOUR_END = 7     # exclusive — 07:00 local
"""Window where proactive push is always blocked, even with no schedule."""


class HeuristicProactiveGate(ProactiveGatePort):
    def __init__(
        self,
        *,
        local_tz: tzinfo | None = None,
        quiet_hour_start: int = _QUIET_HOUR_START,
        quiet_hour_end: int = _QUIET_HOUR_END,
    ) -> None:
        """``local_tz`` is used for the night-hours window.

        Production callers pass the character owner's timezone per
        check; the fallback is UTC so host-local settings cannot leak
        into user-facing civil-time logic.
        """
        self._local_tz = local_tz
        self._quiet_start = max(0, min(23, quiet_hour_start))
        self._quiet_end = max(0, min(24, quiet_hour_end))

    async def check(
        self,
        *,
        character: Character,
        trigger: ProactiveTrigger,
        now: datetime,
        sent_today: int,
        last_attempt_at: datetime | None,
        idle_minutes: float | None,
        current_activity: ScheduleActivity | None,
        local_tz: tzinfo | None = None,
    ) -> GateVerdict:
        # Promise-fulfilment triggers bypass every gate below. Two
        # flavours:
        #
        # * ``PENDING_FOLLOW_UP`` — busy-defer release. The user is
        #   already waiting because the character sent a "I'll get
        #   back to you" earlier.
        # * ``SCHEDULED_PROMISE`` — explicit scheduled message ("明天 10
        #   點叫我起床"). The user asked for this push by name; gates
        #   designed to avoid unsolicited pings don't apply.
        #
        # All the throttles below exist to avoid surprising the user
        # with unsolicited pings — they don't apply when the user
        # explicitly asked first.
        if trigger in (
            ProactiveTrigger.PENDING_FOLLOW_UP,
            ProactiveTrigger.SCHEDULED_PROMISE,
        ):
            return GateVerdict(
                passed=True,
                reason=f"trigger={trigger.value} (promise-fulfilment bypass)",
            )

        if idle_minutes is not None and idle_minutes < _MIN_IDLE_MINUTES:
            return GateVerdict(
                passed=False,
                reason=(
                    f"user active {idle_minutes:.1f}min ago "
                    f"(<{_MIN_IDLE_MINUTES:.0f}min idle threshold)"
                ),
            )

        if sent_today >= character.proactive_daily_limit:
            return GateVerdict(
                passed=False,
                reason=(
                    f"daily limit reached ({sent_today}/"
                    f"{character.proactive_daily_limit})"
                ),
            )

        if last_attempt_at is not None:
            elapsed = now - _ensure_aware(last_attempt_at)
            cooldown = timedelta(minutes=character.proactive_cooldown_minutes)
            if elapsed < cooldown:
                remaining = (cooldown - elapsed).total_seconds() / 60.0
                return GateVerdict(
                    passed=False,
                    reason=(
                        f"cooldown active ({remaining:.1f}min left of "
                        f"{character.proactive_cooldown_minutes}min)"
                    ),
                )

        local_hour = _local_hour(now, local_tz or self._local_tz)
        if _is_quiet_hour(local_hour, self._quiet_start, self._quiet_end):
            return GateVerdict(
                passed=False,
                reason=(
                    f"night-hours floor ({local_hour:02d}:xx local, "
                    f"window {self._quiet_start:02d}:00-{self._quiet_end:02d}:00)"
                ),
            )

        if _is_quiet_activity(current_activity):
            return GateVerdict(
                passed=False,
                reason=(
                    f"character is in a quiet activity "
                    f"({current_activity.category if current_activity else '?'})"
                ),
            )

        if character.state.energy <= _LOW_ENERGY_THRESHOLD:
            return GateVerdict(
                passed=False,
                reason=f"energy {character.state.energy} below threshold",
            )

        return GateVerdict(
            passed=True, reason=f"trigger={trigger.value}",
        )


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _is_quiet_activity(activity: ScheduleActivity | None) -> bool:
    if activity is None:
        return False
    category = (activity.category or "").lower()
    if not category:
        return False
    return any(token in category for token in _QUIET_CATEGORY_TOKENS)


def _local_hour(now: datetime, local_tz: tzinfo | None) -> int:
    aware = _ensure_aware(now)
    if local_tz is not None:
        aware = aware.astimezone(local_tz)
    else:
        aware = aware.astimezone(timezone.utc)
    return aware.hour


def _is_quiet_hour(hour: int, start: int, end: int) -> bool:
    """True when ``hour`` is inside ``[start, end)``.

    Supports wrap-around windows (e.g. start=22, end=6 means 22:00-06:00).
    The degenerate ``start == end`` case is treated as "never quiet" so
    operators can disable the night floor by setting both to the same
    value.
    """
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    # Wrap-around (e.g. 22 → 6): quiet if in [start, 24) or [0, end).
    return hour >= start or hour < end
