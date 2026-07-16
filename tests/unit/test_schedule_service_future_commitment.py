"""Phase 2: future-date commitments lazy-create + plan_day folds them in.

These tests exercise the chat → post-turn → schedule pipeline at the
service boundary: when a ``ScheduleAdjustment`` with a future
``target_date_iso`` lands, the service must (a) create the row with
``is_planned=False`` and the seed activity, (b) feed those seeds back
to ``plan_day`` when ``ensure_schedule`` later runs for that date, and
(c) flip ``is_planned=True`` once the day is fully laid out.
"""

from __future__ import annotations

from datetime import date, datetime, timezone, tzinfo

import pytest

from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.contracts.post_turn import ScheduleAdjustment
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)


UTC = timezone.utc
_TODAY = date(2026, 5, 19)
_TOMORROW = date(2026, 5, 20)


class _RecordingPlanner:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def plan_day(
        self,
        *,
        character: Character,
        date_: date,
        local_tz: tzinfo,
        pre_committed_activities: tuple[ScheduleActivity, ...] = (),
        **_: object,
    ) -> DailySchedule:
        self.calls.append(
            {
                "date": date_,
                "pre_commitments_count": len(pre_committed_activities),
                "pre_commitments": tuple(pre_committed_activities),
            }
        )
        # Mimic a real planner: fill in a couple of generic activities
        # alongside the commitments. The merge step in the LLM planner
        # would dedupe by overlap; in this stub we just append.
        base = datetime.combine(date_, datetime.min.time(), tzinfo=local_tz)
        generated = [
            ScheduleActivity.create(
                start_at=base.replace(hour=8),
                end_at=base.replace(hour=9),
                description="早餐",
                category="meal",
            ),
        ]
        return DailySchedule.create(
            character_id=character.id,
            date_=date_,
            activities=generated + list(pre_committed_activities),
            is_planned=True,
        )


class _FrozenScheduleService(ScheduleService):
    """ScheduleService with today() pinned for deterministic tests."""

    def today(self, now: datetime | None = None) -> date:
        return _TODAY


def _character() -> Character:
    return Character.create(
        name="Aki",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


@pytest.mark.asyncio
async def test_future_add_creates_seed_row_with_is_planned_false() -> None:
    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()
    service = _FrozenScheduleService(
        repository=repo, planner=planner, local_tz=UTC,
    )
    character = _character()

    adjustments = [
        ScheduleAdjustment(
            action="add",
            start="19:00",
            end="21:00",
            description="跟使用者看電影",
            category="leisure",
            target_date_iso="2026-05-20",
        ),
    ]
    await service.apply_adjustments(
        character_id=character.id, adjustments=adjustments,
    )

    stored = await repo.get(character.id, _TOMORROW)
    assert stored is not None
    assert stored.is_planned is False
    assert len(stored.activities) == 1
    assert stored.activities[0].description == "跟使用者看電影"


@pytest.mark.asyncio
async def test_ensure_schedule_folds_seed_into_full_plan() -> None:
    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()
    service = _FrozenScheduleService(
        repository=repo, planner=planner, local_tz=UTC,
    )
    character = _character()

    # Step 1: chat extracts "明天 7 點看電影" → lazy-create.
    await service.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ScheduleAdjustment(
                action="add", start="19:00", end="21:00",
                description="跟使用者看電影", category="leisure",
                target_date_iso="2026-05-20",
            )
        ],
    )

    # Step 2: tick (or chat path) runs ensure_schedule for tomorrow →
    # planner should see the commitment as pre_committed_activities.
    schedule = await service.ensure_schedule(character, date_=_TOMORROW)

    assert planner.calls == [
        {"date": _TOMORROW, "pre_commitments_count": 1,
         "pre_commitments": planner.calls[0]["pre_commitments"]},
    ]
    # The commitment must survive into the final schedule.
    descriptions = {a.description for a in schedule.activities}
    assert "跟使用者看電影" in descriptions
    assert "早餐" in descriptions  # planner added background
    assert schedule.is_planned is True


@pytest.mark.asyncio
async def test_planned_schedule_short_circuits_ensure_schedule() -> None:
    """Fully-planned days must not re-trigger the planner."""
    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()
    service = _FrozenScheduleService(
        repository=repo, planner=planner, local_tz=UTC,
    )
    character = _character()

    await service.ensure_schedule(character, date_=_TOMORROW)
    planner.calls.clear()
    await service.ensure_schedule(character, date_=_TOMORROW)

    assert planner.calls == []


@pytest.mark.asyncio
async def test_remove_modify_for_unknown_future_date_is_ignored() -> None:
    """remove/modify against a non-existent future row must not lazy-
    create — there's nothing to remove or modify yet."""
    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()
    service = _FrozenScheduleService(
        repository=repo, planner=planner, local_tz=UTC,
    )
    character = _character()

    await service.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ScheduleAdjustment(
                action="remove", activity_id="ghost",
                target_date_iso="2026-05-20",
            )
        ],
    )

    assert await repo.get(character.id, _TOMORROW) is None


@pytest.mark.asyncio
async def test_malformed_target_date_falls_back_to_today() -> None:
    """An LLM that emits 'tomorrow' or '2026/05/20' instead of ISO must
    not crash and must not create a future row. The adjustment routes
    to today's bucket (legacy behaviour)."""
    repo = InMemoryScheduleRepository()
    planner = _RecordingPlanner()
    service = _FrozenScheduleService(
        repository=repo, planner=planner, local_tz=UTC,
    )
    character = _character()
    # Pre-create today so apply_adjustments has something to mutate.
    await service.ensure_schedule(character, date_=_TODAY)
    planner.calls.clear()

    await service.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ScheduleAdjustment(
                action="add", start="14:00", end="15:00",
                description="散步", category="leisure",
                target_date_iso="tomorrow",  # malformed
            )
        ],
    )

    # No future row created.
    assert await repo.get(character.id, _TOMORROW) is None
    # Today's row got the new activity.
    today_sched = await repo.get(character.id, _TODAY)
    assert today_sched is not None
    assert any(a.description == "散步" for a in today_sched.activities)
