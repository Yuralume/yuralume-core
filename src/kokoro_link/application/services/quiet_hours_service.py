"""Quiet hours window resolution — per-user override + global fallback.

History: this service was originally keyed to the deployment-wide
``app_runtime_settings`` KV (2026-05-21 HUMANIZATION_ROADMAP §4.5). In
multi-user mode a single quiet window for the whole installation is
wrong — Alice's 02:00–06:00 silence shouldn't bind Bob's character
ticks if Bob keeps a different schedule.

After the 2026-05-26 multi-user phase 2 cleanup the service stores
into ``app_preferences`` via the same ``scoped_preferences`` helpers
that back ``/system/preferences/active-model``:

- ``window(user_id=X)`` → user override → installation-wide default →
  env-driven legacy fallback. Reads cost two KV gets per call (start
  + end); cheap enough that we don't cache.
- ``set_window(start, end, user_id=X)`` writes a per-user override.
  ``user_id=None`` writes the global default — admin-only at the route
  layer.
- ``clear_user_window(user_id)`` drops the override so the global
  default applies again.

The window-membership math (``contains_hour`` / wrap-around) stays in
the dataclass so the rule lives in one place and the
schedule planner / dream tick / future embedding scheduler all agree.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import Optional

from kokoro_link.application.services.scoped_preferences import (
    delete_user_preference,
    get_preference_with_user_fallback,
    set_user_preference,
)
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.repositories import PreferencesRepositoryPort
from kokoro_link.domain.value_objects.timezone import timezone_for_id


KEY_QUIET_HOURS_START = "quiet_hours_start"
KEY_QUIET_HOURS_END = "quiet_hours_end"

# Owner decision (2026-05-21): default 02:00–06:00. Operators override
# via the per-user preference (or the admin global) in-app.
DEFAULT_QUIET_HOURS_START = 2
DEFAULT_QUIET_HOURS_END = 6


def _clamp_hour(value: int) -> int:
    if value < 0:
        return 0
    if value > 23:
        return 23
    return value


def _coerce_hour(raw: object, fallback: int) -> int:
    """Pull an int hour out of whatever the preference store returned.

    Preferences carry typed primitives, but earlier deployments stored
    quiet-hours as strings via ``app_runtime_settings``. Be permissive
    so a legacy DB-restore migration that copied those values in as
    strings still works.
    """
    if isinstance(raw, bool):
        return fallback
    if isinstance(raw, int):
        return _clamp_hour(raw)
    if isinstance(raw, float):
        return _clamp_hour(int(raw))
    if isinstance(raw, str):
        stripped = raw.strip()
        if not stripped:
            return fallback
        try:
            return _clamp_hour(int(stripped))
        except ValueError:
            return fallback
    return fallback


@dataclass(frozen=True, slots=True)
class QuietHoursWindow:
    """Operator-local quiet-hours bounds (inclusive ``start``, exclusive ``end``).

    ``start > end`` represents a window that wraps midnight (e.g. 23–07
    from the legacy dream pass default).
    """

    start: int
    end: int

    def contains_hour(self, hour: int) -> bool:
        if self.start <= self.end:
            return self.start <= hour < self.end
        return hour >= self.start or hour < self.end


class QuietHoursService:
    """Reads the quiet-hours window — user override → global → env fallback.

    Cheap: two KV ``get`` calls per resolution. We don't cache because
    user-facing settings let operators edit live and changes must take
    effect on the next tick rather than after a process restart.
    """

    def __init__(
        self,
        *,
        preferences: PreferencesRepositoryPort | None,
        env_start: int = DEFAULT_QUIET_HOURS_START,
        env_end: int = DEFAULT_QUIET_HOURS_END,
        clock: ClockPort | None = None,
    ) -> None:
        self._preferences = preferences
        self._env_start = _clamp_hour(env_start)
        self._env_end = _clamp_hour(env_end)
        self._clock = clock

    async def window(
        self, *, user_id: Optional[str] = None,
    ) -> QuietHoursWindow:
        """Return the active window for ``user_id``.

        Lookup order: user override → installation-wide default →
        env-driven defaults baked at boot. ``user_id=None`` skips the
        per-user lookup and goes straight to global + env — used by
        background paths that genuinely don't have a user context (and
        by the admin global-write route which reads back its own change).
        """
        if self._preferences is None:
            return QuietHoursWindow(start=self._env_start, end=self._env_end)
        if user_id:
            start_raw = await get_preference_with_user_fallback(
                self._preferences,
                KEY_QUIET_HOURS_START,
                user_id=user_id,
            )
            end_raw = await get_preference_with_user_fallback(
                self._preferences,
                KEY_QUIET_HOURS_END,
                user_id=user_id,
            )
        else:
            start_raw = await self._preferences.get(KEY_QUIET_HOURS_START)
            end_raw = await self._preferences.get(KEY_QUIET_HOURS_END)
        start = _coerce_hour(start_raw, self._env_start)
        end = _coerce_hour(end_raw, self._env_end)
        return QuietHoursWindow(start=start, end=end)

    async def in_quiet_hours(
        self,
        *,
        user_id: Optional[str] = None,
        now: Optional[datetime] = None,
        timezone_id: str | None = None,
        local_tz: tzinfo | None = None,
    ) -> bool:
        window = await self.window(user_id=user_id)
        ref = ensure_utc(
            now if now is not None else (
                self._clock.now()
                if self._clock is not None
                else datetime.now(timezone.utc)
            ),
        )
        target_tz = local_tz
        if target_tz is None and timezone_id:
            target_tz = timezone_for_id(timezone_id)
        if target_tz is not None:
            ref = ref.astimezone(target_tz)
        return window.contains_hour(ref.hour)

    async def set_window(
        self,
        *,
        start: int,
        end: int,
        user_id: Optional[str] = None,
    ) -> QuietHoursWindow:
        """Persist a window. ``user_id=None`` writes the global default;
        otherwise writes a per-user override.

        Route layer is responsible for restricting ``user_id=None``
        writes to admins — see ``/system/preferences/quiet-hours``."""
        clamped_start = _clamp_hour(start)
        clamped_end = _clamp_hour(end)
        if self._preferences is None:
            # In-process fallback path: bump env defaults so subsequent
            # ``window()`` calls reflect the change for this session.
            self._env_start = clamped_start
            self._env_end = clamped_end
            return QuietHoursWindow(start=clamped_start, end=clamped_end)
        if user_id:
            await set_user_preference(
                self._preferences,
                KEY_QUIET_HOURS_START,
                clamped_start,
                user_id=user_id,
            )
            await set_user_preference(
                self._preferences,
                KEY_QUIET_HOURS_END,
                clamped_end,
                user_id=user_id,
            )
        else:
            await self._preferences.set(KEY_QUIET_HOURS_START, clamped_start)
            await self._preferences.set(KEY_QUIET_HOURS_END, clamped_end)
        return QuietHoursWindow(start=clamped_start, end=clamped_end)

    async def clear_user_window(self, *, user_id: str) -> bool:
        """Drop a user's override so the global default applies again.

        Returns True when at least one of the two override keys was
        removed (mirrors the dict-style ``delete`` contract).
        """
        if self._preferences is None or not user_id:
            return False
        removed_start = await delete_user_preference(
            self._preferences,
            KEY_QUIET_HOURS_START,
            user_id=user_id,
        )
        removed_end = await delete_user_preference(
            self._preferences,
            KEY_QUIET_HOURS_END,
            user_id=user_id,
        )
        return removed_start or removed_end


__all__ = [
    "DEFAULT_QUIET_HOURS_END",
    "DEFAULT_QUIET_HOURS_START",
    "KEY_QUIET_HOURS_END",
    "KEY_QUIET_HOURS_START",
    "QuietHoursService",
    "QuietHoursWindow",
]
