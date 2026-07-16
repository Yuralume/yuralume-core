"""Rolling 3-day window: ensure_window + load_upcoming_schedules."""

from __future__ import annotations

from datetime import date, datetime, timezone, tzinfo
from types import SimpleNamespace

import pytest

from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    OPERATOR_INVITE_PENDING_ROLE,
    OPERATOR_WISH_ROLE,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)


UTC = timezone.utc


class _CountingPlanner:
    def __init__(self) -> None:
        self.calls: list[date] = []

    async def plan_day(
        self,
        *,
        character: Character,
        date_: date,
        local_tz: tzinfo,
        **_: object,
    ) -> DailySchedule:
        self.calls.append(date_)
        return DailySchedule.create(
            character_id=character.id,
            date_=date_,
            activities=[
                ScheduleActivity.create(
                    start_at=datetime.combine(
                        date_, datetime.min.time(), tzinfo=local_tz,
                    ).replace(hour=10),
                    end_at=datetime.combine(
                        date_, datetime.min.time(), tzinfo=local_tz,
                    ).replace(hour=11),
                    description=f"day-{date_.isoformat()}",
                    category="work",
                )
            ],
        )


class _OperatorProfileService:
    async def get_for_user(self, user_id: str):  # noqa: ANN001
        return SimpleNamespace(id=user_id, timezone_id="Asia/Taipei")


def _character(*, user_id: str = "default") -> Character:
    return Character.create(
        name="Mio",
        summary="",
        user_id=user_id,
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


@pytest.mark.asyncio
async def test_ensure_window_plans_three_days_by_default() -> None:
    repo = InMemoryScheduleRepository()
    planner = _CountingPlanner()
    service = ScheduleService(repository=repo, planner=planner, local_tz=UTC)
    schedules = await service.ensure_window(
        _character(), start=date(2026, 5, 19),
    )
    assert [s.date for s in schedules] == [
        date(2026, 5, 19), date(2026, 5, 20), date(2026, 5, 21),
    ]
    assert planner.calls == [
        date(2026, 5, 19), date(2026, 5, 20), date(2026, 5, 21),
    ]


@pytest.mark.asyncio
async def test_ensure_window_second_call_is_cached() -> None:
    repo = InMemoryScheduleRepository()
    planner = _CountingPlanner()
    service = ScheduleService(repository=repo, planner=planner, local_tz=UTC)
    character = _character()  # reuse same character — id stable across calls
    await service.ensure_window(character, start=date(2026, 5, 19))
    planner.calls.clear()
    await service.ensure_window(character, start=date(2026, 5, 19))
    # All three days already planned → no second planner call.
    assert planner.calls == []


@pytest.mark.asyncio
async def test_ensure_window_clamps_days_to_seven() -> None:
    repo = InMemoryScheduleRepository()
    planner = _CountingPlanner()
    service = ScheduleService(repository=repo, planner=planner, local_tz=UTC)
    schedules = await service.ensure_window(
        _character(), start=date(2026, 5, 19), days=365,
    )
    assert len(schedules) == 7


@pytest.mark.asyncio
async def test_ensure_window_defaults_to_character_owner_timezone() -> None:
    repo = InMemoryScheduleRepository()
    planner = _CountingPlanner()
    service = ScheduleService(
        repository=repo,
        planner=planner,
        local_tz=UTC,
        operator_profile_service=_OperatorProfileService(),
    )

    schedules = await service.ensure_window(
        _character(user_id="alice"),
        now=datetime(2026, 6, 14, 16, 30, tzinfo=timezone.utc),
    )

    assert [s.date for s in schedules] == [
        date(2026, 6, 15), date(2026, 6, 16), date(2026, 6, 17),
    ]


@pytest.mark.asyncio
async def test_load_upcoming_skips_unplanned_days() -> None:
    repo = InMemoryScheduleRepository()
    planner = _CountingPlanner()
    service = ScheduleService(repository=repo, planner=planner, local_tz=UTC)
    character = _character()
    # Only plan today + day-after, leave tomorrow unplanned.
    await service.ensure_schedule(character, date_=date(2026, 5, 19))
    await service.ensure_schedule(character, date_=date(2026, 5, 21))
    upcoming = await service.load_upcoming_schedules(
        character.id, start_after=date(2026, 5, 19),
    )
    # tomorrow (5/20) absent; day-after (5/21) present.
    assert [s.date for s in upcoming] == [date(2026, 5, 21)]


@pytest.mark.asyncio
async def test_load_upcoming_returns_empty_for_zero_days() -> None:
    repo = InMemoryScheduleRepository()
    planner = _CountingPlanner()
    service = ScheduleService(repository=repo, planner=planner, local_tz=UTC)
    assert (
        await service.load_upcoming_schedules(
            "char-x", start_after=date(2026, 5, 19), days=0,
        )
        == []
    )


def test_resolve_pending_invites_spans_rolling_window() -> None:
    service = ScheduleService(
        repository=InMemoryScheduleRepository(),
        planner=_CountingPlanner(),
        local_tz=UTC,
    )
    today = DailySchedule.create(
        character_id="c1",
        date_=date(2026, 5, 19),
        activities=[
            ScheduleActivity.create(
                start_at=datetime(2026, 5, 19, 9, 0, tzinfo=UTC),
                end_at=datetime(2026, 5, 19, 10, 0, tzinfo=UTC),
                description="已過期邀請",
                category="social",
                participant_refs=(_operator_ref(OPERATOR_INVITE_PENDING_ROLE),),
            ),
        ],
    )
    tomorrow = DailySchedule.create(
        character_id="c1",
        date_=date(2026, 5, 20),
        activities=[
            ScheduleActivity.create(
                start_at=datetime(2026, 5, 20, 19, 0, tzinfo=UTC),
                end_at=datetime(2026, 5, 20, 20, 0, tzinfo=UTC),
                description="明天電影邀請",
                category="social",
                participant_refs=(_operator_ref(OPERATOR_INVITE_PENDING_ROLE),),
            ),
            ScheduleActivity.create(
                start_at=datetime(2026, 5, 20, 21, 0, tzinfo=UTC),
                end_at=datetime(2026, 5, 20, 22, 0, tzinfo=UTC),
                description="只是想著對方",
                category="social",
                participant_refs=(_operator_ref(OPERATOR_WISH_ROLE),),
            ),
        ],
    )

    pending = service.resolve_pending_invites_from_schedules(
        [today, tomorrow],
        now=datetime(2026, 5, 19, 12, 0, tzinfo=UTC),
    )

    assert [activity.description for activity in pending] == ["明天電影邀請"]


def _operator_ref(role: str) -> ParticipantRef:
    return ParticipantRef(
        actor_kind="operator",
        actor_id=None,
        display_name="使用者",
        role=role,
    )
