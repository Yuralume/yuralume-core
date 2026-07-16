"""Tick-time due-beat scanner for story arcs.

The chat path stages a due beat in the prompt, then post-turn realizes
it only if the scene actually happened. This checker gives the
proactive scheduler the same signal. When the Direction C autonomous
scene service is wired, required beats can also be completed as short
scenes without waiting for the user; otherwise a due beat can earn an
ARC_BEAT notification attempt and remains pending.

Design choices:

- **Scanning runs for every character**, regardless of
  ``proactive_enabled``. It records that the system noticed the due beat
  and provides attempt facts for the LLM; flipping proactive off should
  not hide that the beat is waiting.
- **Notification only when the beat is required + character opted in**.
  Optional / colour beats stay silent; they show up next time the user
  opens chat. Required beats with proactive enabled get a
  ``ProactiveTrigger.ARC_BEAT`` signal so the dispatcher can decide
  whether to ping.
- **Scene realization is optional wiring**. If ``StoryBeatSceneService``
  is present, the checker lets it write an autonomous scene first; if
  that fails, the old notification-candidate behavior remains.
- **Fail-soft everywhere**. A planner crash on one character must not
  stop the scheduler from sweeping the rest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import TYPE_CHECKING

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.timezone import timezone_for_id

if TYPE_CHECKING:
    from kokoro_link.application.services.story_arc_service import StoryArcService
    from kokoro_link.application.services.story_beat_scene_service import (
        StoryBeatSceneService,
    )
    from kokoro_link.application.services.story_event_service import StoryEventService

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BeatScanResult:
    """Outcome of one ``BeatDueChecker.scan(character)`` pass.

    The scheduler reads ``should_notify`` to decide whether to enqueue
    a ``ProactiveTrigger.ARC_BEAT`` event; everything else is for
    logging / telemetry.
    """

    attempted_beat_id: str | None
    """Id of the beat surfaced by this scan, or ``None`` when nothing
    was due."""
    should_notify: bool
    """``True`` when the dispatcher should consider a proactive ping."""
    realized_event_id: str | None = None
    """StoryEvent id when an autonomous scene completed this beat."""

    @classmethod
    def empty(cls) -> "BeatScanResult":
        return cls(
            attempted_beat_id=None,
            should_notify=False,
            realized_event_id=None,
        )


class BeatDueChecker:
    """Service object the scheduler calls per character per tick.

    Stateless — picking up a fresh service instance each tick would be
    fine; the singleton just saves allocations.
    """

    def __init__(
        self,
        *,
        story_event_service: "StoryEventService",
        story_arc_service: "StoryArcService",
        story_beat_scene_service: "StoryBeatSceneService | None" = None,
        local_tz: tzinfo | None = None,
        operator_profile_service=None,  # noqa: ANN001 - optional owner timezone resolver
    ) -> None:
        # Kept in the constructor for wiring compatibility and for the
        # optional autonomous scene service's StoryEvent landing path.
        self._events = story_event_service
        self._arcs = story_arc_service
        self._scene_service = story_beat_scene_service
        self._local_tz = local_tz
        self._operator_profile_service = operator_profile_service

    async def scan(
        self,
        character: Character,
        *,
        now: datetime | None = None,
    ) -> BeatScanResult:
        today = await self._today_for_character(character, now)

        # Cheap pre-check: skip the (potentially expensive) ensure_today
        # if there's nothing arc-side to materialise. ensure_today
        # would still produce a gacha event in this case, but that's
        # the chat path's job — keeping the tick loop arc-only avoids
        # accidentally rolling daily gacha for users who just haven't
        # opened chat yet today.
        try:
            due = await self._arcs.next_beat_due(character.id, today=today)
        except Exception:
            _LOGGER.exception(
                "beat-due check: next_beat_due crashed character=%s",
                character.id,
            )
            return BeatScanResult.empty()
        if due is None:
            return BeatScanResult.empty()
        _arc, beat = due

        if self._scene_service is not None:
            try:
                event = await self._scene_service.play_beat(
                    character,
                    beat_id=beat.id,
                    now=now,
                )
            except Exception:
                _LOGGER.exception(
                    "beat-due check: autonomous scene crashed character=%s beat=%s",
                    character.id, beat.id,
                )
                event = None
            if event is not None:
                return BeatScanResult(
                    attempted_beat_id=beat.id,
                    should_notify=False,
                    realized_event_id=event.id,
                )

        try:
            await self._arcs.mark_beat_play_attempted(
                beat_id=beat.id,
                attempted_at=now or datetime.now(timezone.utc),
                source="proactive_tick",
                result="notification_candidate",
                push_intensity="proactive_candidate",
            )
        except Exception:
            _LOGGER.exception(
                "beat-due check: play-attempt record crashed character=%s beat=%s",
                character.id, beat.id,
            )
            return BeatScanResult.empty()

        should_notify = bool(beat.required) and bool(character.proactive_enabled)
        return BeatScanResult(
            attempted_beat_id=beat.id,
            should_notify=should_notify,
            realized_event_id=None,
        )

    async def _today_for_character(self, character: Character, now: datetime | None):
        when = now or datetime.now(timezone.utc)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        local_tz = await self._resolve_operator_timezone(character)
        return when.astimezone(local_tz).date()

    async def _resolve_operator_timezone(self, character: Character) -> tzinfo:
        default = self._local_tz or timezone.utc
        service = self._operator_profile_service
        if service is None:
            return default
        user_id = getattr(character, "user_id", None) or "default"
        try:
            operator = await service.get_for_user(user_id)
            return timezone_for_id(getattr(operator, "timezone_id", None))
        except Exception:  # pragma: no cover - defensive
            return default
