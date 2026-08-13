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
- **Beats about the player are left for the player**. When the scene
  service is wired the scan is *unattended* play, so a beat whose
  ``operator_position`` is ``central`` is passed over
Passing over costs the beat
  nothing — no attempt, no failure, no backoff, no retirement — because
  it did not fail; it is waiting. The player pulling the same beat
  through 起幕 or chat plays it normally.
- **Fail-soft everywhere**. A planner crash on one character must not
  stop the scheduler from sweeping the rest.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date as date_type, datetime, timezone, tzinfo
from typing import TYPE_CHECKING

from kokoro_link.application.services.beat_retry_policy import (
    AUTONOMOUS_SCENE_RETRY_POLICY,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.story_arc import PLAY_RESULT_FAILED
from kokoro_link.domain.value_objects.timezone import timezone_for_id

if TYPE_CHECKING:
    from kokoro_link.application.services.story_arc_service import StoryArcService
    from kokoro_link.domain.entities.story_arc import StoryArcBeat
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
        attempted_at = now or datetime.now(timezone.utc)

        # Retire first, stage second. ``next_beat_due`` is a read path
        # shared with chat, so the permanent skip lives here — on the one
        # caller that applies the retry policy and already owns write
        # side effects. Gated on the same condition as the policy itself:
        # without an autonomous scene service no budget is being spent,
        # so nothing may be declared dead for having spent it.
        if self._scene_service is not None:
            await self._retire_exhausted_beats(character, today=today)

        try:
            due = await self._arcs.next_beat_due(
                character.id,
                today=today,
                retry_policy=(
                    AUTONOMOUS_SCENE_RETRY_POLICY
                    if self._scene_service is not None
                    else None
                ),
                retry_at=attempted_at,
                # Only this configuration *plays* a beat with nobody in
                # the room, so only it must pass over the beats that are
                # about the player (OP2-B). Without a scene service the
                # scan's whole output is a notification candidate — an
                # invitation for the player to come — which is precisely
                # what a ``central`` beat is waiting for, so suppressing
                # it there would silence the one path that helps.
                unattended=self._scene_service is not None,
            )
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
            if not await self._scene_attempt_was_recorded(beat):
                try:
                    await self._arcs.mark_beat_play_attempted(
                        beat_id=beat.id,
                        attempted_at=attempted_at,
                        source="scene_simulation",
                        result=PLAY_RESULT_FAILED,
                        push_intensity="autonomous_scene",
                    )
                except Exception:
                    _LOGGER.exception(
                        "beat-due check: scene failure record crashed "
                        "character=%s beat=%s",
                        character.id,
                        beat.id,
                    )
                    return BeatScanResult.empty()
            return BeatScanResult(
                attempted_beat_id=beat.id,
                should_notify=(
                    bool(beat.required) and bool(character.proactive_enabled)
                ),
                realized_event_id=None,
            )

        try:
            await self._arcs.mark_beat_play_attempted(
                beat_id=beat.id,
                attempted_at=attempted_at,
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

    async def next_due_at(
        self,
        character: Character,
        now: datetime | None = None,
    ) -> datetime | None:
        """Precise UTC instant of the character's next FUTURE story beat.

        The distributed ``beat_due`` chain (§ beat_due precision) passes this as
        ``explicit_next_due`` so a beat scheduled days out does not incur 288
        pointless 300s rechecks. Returns the operator-local day START of the next
        pending beat whose ``scheduled_date`` is strictly after today; ``None``
        when nothing is scheduled ahead (beat due today / no arc) so the chain
        keeps its base 300s recheck. Fail-soft: any error → ``None`` (fallback).
        """
        try:
            local_tz = await self._resolve_operator_timezone(character)
            today = await self._today_for_character(character, now)
            future_date = await self._arcs.next_future_beat_date(
                character.id, after=today,
            )
        except Exception:
            _LOGGER.exception(
                "beat-due check: next_due_at crashed character=%s", character.id,
            )
            return None
        if future_date is None:
            return None
        local_midnight = datetime(
            future_date.year, future_date.month, future_date.day,
            tzinfo=local_tz,
        )
        return local_midnight.astimezone(timezone.utc)

    async def _retire_exhausted_beats(
        self,
        character: Character,
        *,
        today: date_type,
    ) -> None:
        """Let the arc move past beats whose failure budget is spent.

        Fail-soft: a retirement write that blows up must not cost this
        character its scan.
        """
        try:
            await self._arcs.retire_exhausted_beats(
                character.id,
                retry_policy=AUTONOMOUS_SCENE_RETRY_POLICY,
                today=today,
            )
        except Exception:
            _LOGGER.exception(
                "beat-due check: exhausted-beat retirement crashed "
                "character=%s",
                character.id,
            )

    async def _scene_attempt_was_recorded(
        self,
        previous: "StoryArcBeat",
    ) -> bool:
        """Did the scene service already record its own failed attempt?

        Reads the played beat by id rather than re-deriving it from
        ``next_beat_due``: since the retry walk-down (§10 #4) the beat we
        played is not necessarily the head of the due list any more, and
        comparing against the head would record a duplicate failure —
        charging the retry budget twice for one failure.
        """
        try:
            refreshed = await self._arcs.find_beat(previous.id)
        except Exception:
            _LOGGER.exception(
                "beat-due check: failed to verify scene attempt beat=%s",
                previous.id,
            )
            return False
        if refreshed is None:
            return True
        return refreshed.play_attempt_count > previous.play_attempt_count

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
