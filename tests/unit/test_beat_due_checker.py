"""BeatDueChecker BDD.

Tick-time due-beat scanner that records an attempt and optionally
enqueues a proactive ARC_BEAT signal. When the autonomous scene service
is wired, it may complete the beat as a StoryEvent without waiting for
the user; otherwise it keeps the Direction B notification-candidate
behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from kokoro_link.application.services.beat_due_checker import (
    BeatDueChecker,
    BeatScanResult,
)
from kokoro_link.application.services.beat_retry_policy import (
    AUTONOMOUS_SCENE_RETRY_POLICY,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.story_arc import (
    StoryArc,
    StoryArcBeat,
    TENSION_RISING,
)
from kokoro_link.domain.value_objects.character_state import CharacterState

UTC = timezone.utc


@dataclass
class _StubArcService:
    next_beat_due_response: tuple[StoryArc, StoryArcBeat] | None = None
    find_beat_response: StoryArcBeat | None = None
    crash_on_next_beat_due: bool = False
    crash_on_mark_attempt: bool = False
    crash_on_retire: bool = False
    next_beat_due_calls: int = 0
    mark_attempt_calls: int = 0
    retire_calls: int = 0
    call_order: list[str] = field(default_factory=list)
    last_today: date | None = None
    last_retire_today: date | None = None
    last_retire_policy: Any = None
    last_marked_beat_id: str | None = None
    last_mark_kwargs: dict | None = None
    last_find_beat_id: str | None = None
    last_retry_policy: Any = None
    last_unattended: bool | None = None

    async def next_beat_due(
        self,
        character_id: str,
        *,
        today: date,  # noqa: ARG002
        retry_policy=None,  # noqa: ANN001
        retry_at=None,  # noqa: ANN001, ARG002
        unattended: bool = False,
    ) -> tuple[StoryArc, StoryArcBeat] | None:
        self.next_beat_due_calls += 1
        self.call_order.append("next_beat_due")
        self.last_today = today
        self.last_retry_policy = retry_policy
        self.last_unattended = unattended
        if self.crash_on_next_beat_due:
            raise RuntimeError("planner exploded")
        return self.next_beat_due_response

    async def retire_exhausted_beats(
        self,
        character_id: str,  # noqa: ARG002
        *,
        retry_policy,  # noqa: ANN001
        today: date,
    ):
        self.retire_calls += 1
        self.call_order.append("retire_exhausted_beats")
        self.last_retire_today = today
        self.last_retire_policy = retry_policy
        if self.crash_on_retire:
            raise RuntimeError("retirement write exploded")
        return None

    async def find_beat(self, beat_id: str) -> StoryArcBeat | None:
        self.last_find_beat_id = beat_id
        if self.find_beat_response is not None:
            return self.find_beat_response
        response = self.next_beat_due_response
        if response is None:
            return None
        _arc, beat = response
        return beat if beat.id == beat_id else None

    async def mark_beat_play_attempted(self, **kwargs):
        self.mark_attempt_calls += 1
        self.last_marked_beat_id = kwargs.get("beat_id")
        self.last_mark_kwargs = dict(kwargs)
        if self.crash_on_mark_attempt:
            raise RuntimeError("attempt write exploded")
        return None


@dataclass
class _UnusedEventService:
    calls: int = 0

    async def ensure_today(self, *args, **kwargs):  # noqa: ANN002, ANN003
        self.calls += 1
        raise AssertionError("BeatDueChecker must not materialize events")


@dataclass
class _StubSceneService:
    event_id: str | None = None
    crash: bool = False
    calls: int = 0
    last_beat_id: str | None = None

    async def play_beat(self, character, *, beat_id: str, now=None):  # noqa: ANN001
        self.calls += 1
        self.last_beat_id = beat_id
        if self.crash:
            raise RuntimeError("scene service exploded")
        if self.event_id is None:
            return None
        return SimpleNamespace(id=self.event_id)


def _character(*, proactive_enabled: bool = True) -> Character:
    return Character.create(
        name="Aki", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        proactive_enabled=proactive_enabled,
    )


class _OperatorProfileService:
    async def get_for_user(self, user_id: str) -> OperatorProfile:
        return OperatorProfile(
            id=user_id,
            display_name=user_id,
            timezone_id="Asia/Taipei",
        )


def _beat(
    *,
    beat_id: str = "beat-1",
    required: bool = True,
    today: date = date(2026, 5, 1),
) -> StoryArcBeat:
    return StoryArcBeat.create(
        arc_id="arc-1",
        sequence=0,
        scheduled_date=today,
        title="第一場戲",
        summary="今天是關鍵的一天。",
        tension=TENSION_RISING,
        required=required,
        id=beat_id,
    )


def _arc(beat: StoryArcBeat) -> StoryArc:
    arc = StoryArc.create(
        character_id="char-1",
        title="主軸",
        premise="主軸前情。",
        theme="ambition",
        start_date=beat.scheduled_date,
        end_date=beat.scheduled_date,
    )
    return arc.with_beats([beat])


def _checker(
    arc_service: Any,
    event_service: Any,
    *,
    scene_service: Any = None,
) -> BeatDueChecker:
    return BeatDueChecker(
        story_event_service=event_service,
        story_arc_service=arc_service,
        story_beat_scene_service=scene_service,
        local_tz=UTC,
    )


def _checker_with_owner_tz(arc_service: Any, event_service: Any) -> BeatDueChecker:
    return BeatDueChecker(
        story_event_service=event_service,
        story_arc_service=arc_service,
        local_tz=UTC,
        operator_profile_service=_OperatorProfileService(),
    )


@pytest.mark.asyncio
async def test_no_arc_due_returns_empty_and_skips_event_materialization() -> None:
    arc_service = _StubArcService(next_beat_due_response=None)
    event_service = _UnusedEventService()
    checker = _checker(arc_service, event_service)

    result = await checker.scan(
        _character(),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert result == BeatScanResult.empty()
    assert event_service.calls == 0
    assert arc_service.next_beat_due_calls == 1
    assert arc_service.mark_attempt_calls == 0


@pytest.mark.asyncio
async def test_due_beat_records_attempt_and_notifies_when_required() -> None:
    beat = _beat(required=True)
    arc = _arc(beat)
    arc_service = _StubArcService(next_beat_due_response=(arc, beat))
    event_service = _UnusedEventService()
    checker = _checker(arc_service, event_service)

    result = await checker.scan(
        _character(proactive_enabled=True),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert event_service.calls == 0
    assert arc_service.mark_attempt_calls == 1
    assert arc_service.last_marked_beat_id == beat.id
    assert arc_service.last_mark_kwargs is not None
    assert arc_service.last_mark_kwargs["source"] == "proactive_tick"
    assert result.attempted_beat_id == beat.id
    assert result.should_notify is True
    assert result.realized_event_id is None


@pytest.mark.asyncio
async def test_due_beat_scene_service_realizes_without_notifying() -> None:
    beat = _beat(required=True)
    arc = _arc(beat)
    arc_service = _StubArcService(next_beat_due_response=(arc, beat))
    scene_service = _StubSceneService(event_id="event-1")
    checker = _checker(
        arc_service,
        _UnusedEventService(),
        scene_service=scene_service,
    )

    result = await checker.scan(
        _character(proactive_enabled=True),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert scene_service.calls == 1
    assert scene_service.last_beat_id == beat.id
    assert arc_service.mark_attempt_calls == 0
    assert result.attempted_beat_id == beat.id
    assert result.should_notify is False
    assert result.realized_event_id == "event-1"


@pytest.mark.asyncio
async def test_scene_service_failure_falls_back_to_notification_candidate() -> None:
    beat = _beat(required=True)
    arc = _arc(beat)
    arc_service = _StubArcService(next_beat_due_response=(arc, beat))
    checker = _checker(
        arc_service,
        _UnusedEventService(),
        scene_service=_StubSceneService(crash=True),
    )

    result = await checker.scan(
        _character(proactive_enabled=True),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert arc_service.mark_attempt_calls == 1
    assert arc_service.last_mark_kwargs is not None
    assert arc_service.last_mark_kwargs["source"] == "scene_simulation"
    assert arc_service.last_mark_kwargs["result"] == "failed"
    assert result.attempted_beat_id == beat.id
    assert result.should_notify is True
    assert result.realized_event_id is None


@pytest.mark.asyncio
async def test_due_beat_optional_does_not_notify() -> None:
    beat = _beat(required=False)
    arc = _arc(beat)
    arc_service = _StubArcService(next_beat_due_response=(arc, beat))
    checker = _checker(arc_service, _UnusedEventService())

    result = await checker.scan(
        _character(proactive_enabled=True),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert arc_service.mark_attempt_calls == 1
    assert result.attempted_beat_id == beat.id
    assert result.should_notify is False


@pytest.mark.asyncio
async def test_proactive_disabled_records_attempt_without_notify() -> None:
    beat = _beat(required=True)
    arc = _arc(beat)
    arc_service = _StubArcService(next_beat_due_response=(arc, beat))
    checker = _checker(arc_service, _UnusedEventService())

    result = await checker.scan(
        _character(proactive_enabled=False),
        now=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert arc_service.mark_attempt_calls == 1
    assert result.attempted_beat_id == beat.id
    assert result.should_notify is False


@pytest.mark.asyncio
async def test_arc_service_crash_is_fail_soft() -> None:
    arc_service = _StubArcService(crash_on_next_beat_due=True)
    event_service = _UnusedEventService()
    checker = _checker(arc_service, event_service)

    result = await checker.scan(_character())

    assert result == BeatScanResult.empty()
    assert event_service.calls == 0


@pytest.mark.asyncio
async def test_mark_attempt_crash_is_fail_soft() -> None:
    beat = _beat(required=True)
    arc = _arc(beat)
    arc_service = _StubArcService(
        next_beat_due_response=(arc, beat),
        crash_on_mark_attempt=True,
    )
    checker = _checker(arc_service, _UnusedEventService())

    result = await checker.scan(_character())

    assert result == BeatScanResult.empty()
    assert arc_service.mark_attempt_calls == 1


# --- SC0: exhausted-beat retirement (§10 #2) --------------------------


@pytest.mark.asyncio
async def test_scan_retires_exhausted_beats_before_staging() -> None:
    beat = _beat(required=True)
    arc_service = _StubArcService(next_beat_due_response=(_arc(beat), beat))
    checker = _checker(
        arc_service,
        _UnusedEventService(),
        scene_service=_StubSceneService(event_id="event-1"),
    )

    await checker.scan(_character(), now=datetime(2026, 5, 1, tzinfo=UTC))

    assert arc_service.retire_calls == 1
    # Retirement must run first, or the scan stages a beat the policy is
    # about to declare dead.
    assert arc_service.call_order[0] == "retire_exhausted_beats"
    assert arc_service.last_retire_today == date(2026, 5, 1)
    # Same budget the staging gate uses — retiring on a policy the scan
    # does not apply would kill live beats.
    assert arc_service.last_retire_policy is AUTONOMOUS_SCENE_RETRY_POLICY


@pytest.mark.asyncio
async def test_scan_without_scene_service_never_retires() -> None:
    beat = _beat(required=True)
    arc_service = _StubArcService(next_beat_due_response=(_arc(beat), beat))
    checker = _checker(arc_service, _UnusedEventService())

    await checker.scan(_character(), now=datetime(2026, 5, 1, tzinfo=UTC))

    # No autonomous scene service → no retry policy is applied → no beat
    # may be retired on the strength of one.
    assert arc_service.retire_calls == 0


@pytest.mark.asyncio
async def test_retirement_crash_does_not_stop_the_scan() -> None:
    beat = _beat(required=True)
    arc_service = _StubArcService(
        next_beat_due_response=(_arc(beat), beat),
        crash_on_retire=True,
    )
    scene_service = _StubSceneService(event_id="event-1")
    checker = _checker(
        arc_service, _UnusedEventService(), scene_service=scene_service,
    )

    result = await checker.scan(
        _character(), now=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert scene_service.calls == 1
    assert result.realized_event_id == "event-1"


@pytest.mark.asyncio
async def test_scene_failure_verification_reads_the_played_beat() -> None:
    """§10 #4 fallout: the played beat is no longer always the due head.

    With the walk-down, ``next_beat_due`` can hand back ``candidates[1]``.
    Verifying "did the scene service already record its failure?" by
    re-reading the head would compare the wrong beat and double-charge
    the retry budget, which is the very over-suppression SC0 removes.
    """
    played = _beat(beat_id="beat-2")
    arc_service = _StubArcService(
        next_beat_due_response=(_arc(played), played),
        # The scene service already recorded the failed attempt.
        find_beat_response=played.with_play_attempt(
            attempted_at=datetime(2026, 5, 1, tzinfo=UTC),
            source="scene_simulation",
            result="writer_crashed",
            push_intensity="autonomous_scene",
        ),
    )
    checker = _checker(
        arc_service,
        _UnusedEventService(),
        scene_service=_StubSceneService(event_id=None),
    )

    result = await checker.scan(
        _character(), now=datetime(2026, 5, 1, tzinfo=UTC),
    )

    assert arc_service.last_find_beat_id == played.id
    assert arc_service.mark_attempt_calls == 0
    assert result.attempted_beat_id == played.id


# --- OP2-B: the scan declares whether anyone is in the room -----------


@pytest.mark.asyncio
async def test_scan_that_can_play_a_scene_asks_for_unattended_candidates(
) -> None:
    """With a scene service the scan performs; ``central`` beats cannot.

    The flag and the retry policy ride the same gate on purpose: both
    only make sense for the configuration that actually plays a beat
    with nobody watching.
    """
    beat = _beat(required=True)
    arc_service = _StubArcService(next_beat_due_response=(_arc(beat), beat))
    checker = _checker(
        arc_service,
        _UnusedEventService(),
        scene_service=_StubSceneService(event_id="event-1"),
    )

    await checker.scan(_character(), now=datetime(2026, 5, 1, tzinfo=UTC))

    assert arc_service.last_unattended is True
    assert arc_service.last_retry_policy is AUTONOMOUS_SCENE_RETRY_POLICY


@pytest.mark.asyncio
async def test_scan_without_a_scene_service_still_wants_central_beats() -> None:
    """Nothing is performed here — the output is an invitation.

    Without the scene service the scan's entire product is a
    notification candidate asking the player to come, which is exactly
    what a ``central`` beat is waiting for. Suppressing it would silence
    the one path that helps.
    """
    beat = _beat(required=True)
    arc_service = _StubArcService(next_beat_due_response=(_arc(beat), beat))
    checker = _checker(arc_service, _UnusedEventService())

    await checker.scan(_character(), now=datetime(2026, 5, 1, tzinfo=UTC))

    assert arc_service.last_unattended is False
    assert arc_service.last_retry_policy is None


@pytest.mark.asyncio
async def test_scan_uses_owner_timezone_for_due_day_boundary() -> None:
    today_owner = date(2026, 6, 15)
    beat = _beat(required=True, today=today_owner)
    arc = _arc(beat)
    arc_service = _StubArcService(next_beat_due_response=(arc, beat))
    checker = _checker_with_owner_tz(arc_service, _UnusedEventService())
    character = _character(proactive_enabled=True)
    from dataclasses import replace
    character = replace(character, user_id="owner-tw")

    result = await checker.scan(
        character,
        now=datetime(2026, 6, 14, 16, 30, tzinfo=UTC),
    )

    assert arc_service.last_today == today_owner
    assert arc_service.last_marked_beat_id == beat.id
    assert result.should_notify is True
