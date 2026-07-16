"""Tests for ScheduleService.apply_adjustments (Phase 2.3)."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.contracts.post_turn import ScheduleAdjustment
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    OPERATOR_CONFIRMED_SHARED_ROLE,
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


class _NoopPlanner:
    async def plan_day(self, **_):
        raise NotImplementedError


def _character() -> Character:
    return Character.create(
        name="Aki", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )


async def _seed_schedule(
    service: ScheduleService,
    character_id: str,
    *,
    date_: date,
    activities: list[ScheduleActivity],
) -> DailySchedule:
    schedule = DailySchedule.create(
        character_id=character_id, date_=date_, activities=activities,
    )
    await service._repository.save(schedule)  # noqa: SLF001 — test helper
    return schedule


def _activity(h_start: int, h_end: int, description: str, *, memorialized: bool = False) -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, h_start, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 18, h_end, 0, tzinfo=UTC),
        description=description,
        category="work",
        memorialized=memorialized,
    )


def _build_service() -> ScheduleService:
    return ScheduleService(
        repository=InMemoryScheduleRepository(),
        planner=_NoopPlanner(),
        local_tz=UTC,
    )


@pytest.mark.asyncio
async def test_remove_drops_activity() -> None:
    service = _build_service()
    character = _character()
    schedule = await _seed_schedule(
        service, character.id, date_=date(2026, 4, 18),
        activities=[_activity(9, 12, "morning"), _activity(14, 18, "afternoon")],
    )
    target_id = schedule.activities[0].id

    updated = await service.apply_adjustments(
        character_id=character.id,
        adjustments=[ScheduleAdjustment(action="remove", activity_id=target_id)],
        date_=date(2026, 4, 18),
    )
    assert updated is not None
    assert len(updated.activities) == 1
    assert updated.activities[0].description == "afternoon"


@pytest.mark.asyncio
async def test_remove_preserves_memorialized_activity() -> None:
    service = _build_service()
    character = _character()
    schedule = await _seed_schedule(
        service, character.id, date_=date(2026, 4, 18),
        activities=[_activity(9, 12, "done", memorialized=True)],
    )
    target_id = schedule.activities[0].id

    updated = await service.apply_adjustments(
        character_id=character.id,
        adjustments=[ScheduleAdjustment(action="remove", activity_id=target_id)],
        date_=date(2026, 4, 18),
    )
    # Memorialized activity was not removed → no mutation → None
    assert updated is None


@pytest.mark.asyncio
async def test_add_creates_new_block() -> None:
    service = _build_service()
    character = _character()
    await _seed_schedule(
        service, character.id, date_=date(2026, 4, 18),
        activities=[_activity(9, 12, "morning")],
    )

    updated = await service.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ScheduleAdjustment(
                action="add", start="19:00", end="20:30",
                description="晚上跟朋友吃飯", category="social",
                location="餐廳", busy_score=0.3,
            )
        ],
        date_=date(2026, 4, 18),
    )
    assert updated is not None
    assert len(updated.activities) == 2
    dinner = next(a for a in updated.activities if "吃飯" in a.description)
    assert dinner.category == "social"
    assert dinner.busy_score == 0.3


@pytest.mark.asyncio
async def test_add_activity_persists_operator_involvement_ref() -> None:
    service = _build_service()
    character = _character()
    await _seed_schedule(
        service, character.id, date_=date(2026, 4, 18),
        activities=[_activity(9, 12, "morning")],
    )

    updated = await service.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ScheduleAdjustment(
                action="add",
                start="19:00",
                end="20:30",
                description="看電影",
                category="social",
                operator_involvement=OPERATOR_CONFIRMED_SHARED_ROLE,
                operator_display_name="小悠",
            ),
        ],
        date_=date(2026, 4, 18),
    )

    assert updated is not None
    movie = next(a for a in updated.activities if a.description == "看電影")
    assert movie.participant_refs == (
        ParticipantRef(
            actor_kind="operator",
            actor_id=None,
            display_name="小悠",
            role=OPERATOR_CONFIRMED_SHARED_ROLE,
        ),
    )


@pytest.mark.asyncio
async def test_add_rejects_incomplete_payload() -> None:
    service = _build_service()
    character = _character()
    await _seed_schedule(
        service, character.id, date_=date(2026, 4, 18),
        activities=[_activity(9, 12, "morning")],
    )

    updated = await service.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ScheduleAdjustment(action="add", start="19:00", end="20:00"),  # no desc/category
            ScheduleAdjustment(action="add", description="x", category="y"),  # no times
        ],
        date_=date(2026, 4, 18),
    )
    assert updated is None


@pytest.mark.asyncio
async def test_modify_updates_specified_fields_only() -> None:
    service = _build_service()
    character = _character()
    schedule = await _seed_schedule(
        service, character.id, date_=date(2026, 4, 18),
        activities=[_activity(14, 18, "會議")],
    )
    target_id = schedule.activities[0].id

    updated = await service.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ScheduleAdjustment(
                action="modify", activity_id=target_id,
                end="19:00", description="會議延長到晚上",
            )
        ],
        date_=date(2026, 4, 18),
    )
    assert updated is not None
    modified = updated.activities[0]
    assert modified.end_at.hour == 19
    assert modified.description == "會議延長到晚上"
    # Fields not mentioned should stay
    assert modified.category == "work"


@pytest.mark.asyncio
async def test_modify_can_upgrade_pending_invite_to_confirmed_shared() -> None:
    service = _build_service()
    character = _character()
    pending = ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, 19, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 18, 20, 0, tzinfo=UTC),
        description="看電影",
        category="social",
        participant_refs=(
            ParticipantRef(
                actor_kind="operator",
                actor_id=None,
                display_name="使用者",
                role=OPERATOR_INVITE_PENDING_ROLE,
            ),
        ),
    )
    schedule = await _seed_schedule(
        service, character.id, date_=date(2026, 4, 18), activities=[pending],
    )

    updated = await service.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ScheduleAdjustment(
                action="modify",
                activity_id=schedule.activities[0].id,
                operator_involvement=OPERATOR_CONFIRMED_SHARED_ROLE,
                operator_display_name="小悠",
            ),
        ],
        date_=date(2026, 4, 18),
    )

    assert updated is not None
    assert updated.activities[0].participant_refs[0].role == OPERATOR_CONFIRMED_SHARED_ROLE
    assert updated.activities[0].participant_refs[0].display_name == "小悠"


def test_resolve_pending_invites_returns_unexpired_invite_pending_only() -> None:
    service = _build_service()
    schedule = DailySchedule.create(
        character_id="c1",
        date_=date(2026, 4, 18),
        activities=[
            ScheduleActivity.create(
                start_at=datetime(2026, 4, 18, 9, 0, tzinfo=UTC),
                end_at=datetime(2026, 4, 18, 10, 0, tzinfo=UTC),
                description="過期邀請",
                category="social",
                participant_refs=(
                    ParticipantRef(
                        actor_kind="operator",
                        actor_id=None,
                        display_name="使用者",
                        role=OPERATOR_INVITE_PENDING_ROLE,
                    ),
                ),
            ),
            ScheduleActivity.create(
                start_at=datetime(2026, 4, 18, 19, 0, tzinfo=UTC),
                end_at=datetime(2026, 4, 18, 20, 0, tzinfo=UTC),
                description="未確認電影邀請",
                category="social",
                participant_refs=(
                    ParticipantRef(
                        actor_kind="operator",
                        actor_id=None,
                        display_name="使用者",
                        role=OPERATOR_INVITE_PENDING_ROLE,
                    ),
                ),
            ),
            ScheduleActivity.create(
                start_at=datetime(2026, 4, 18, 21, 0, tzinfo=UTC),
                end_at=datetime(2026, 4, 18, 22, 0, tzinfo=UTC),
                description="只是想著對方",
                category="social",
                participant_refs=(
                    ParticipantRef(
                        actor_kind="operator",
                        actor_id=None,
                        display_name="使用者",
                        role=OPERATOR_WISH_ROLE,
                    ),
                ),
            ),
        ],
    )

    pending = service.resolve_pending_invites(
        schedule,
        now=datetime(2026, 4, 18, 12, 0, tzinfo=UTC),
    )

    assert [activity.description for activity in pending] == ["未確認電影邀請"]


@pytest.mark.asyncio
async def test_modify_skips_memorialized_activity() -> None:
    service = _build_service()
    character = _character()
    schedule = await _seed_schedule(
        service, character.id, date_=date(2026, 4, 18),
        activities=[_activity(9, 12, "done", memorialized=True)],
    )
    target_id = schedule.activities[0].id

    updated = await service.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ScheduleAdjustment(
                action="modify", activity_id=target_id,
                description="rewrite history",
            )
        ],
        date_=date(2026, 4, 18),
    )
    # memorialized blocks are untouched → no mutation
    assert updated is None


@pytest.mark.asyncio
async def test_returns_none_when_no_schedule_for_date() -> None:
    service = _build_service()
    character = _character()
    updated = await service.apply_adjustments(
        character_id=character.id,
        adjustments=[
            ScheduleAdjustment(action="add", start="09:00", end="10:00",
                               description="x", category="y")
        ],
        date_=date(2026, 4, 18),
    )
    assert updated is None


@pytest.mark.asyncio
async def test_empty_adjustments_list_short_circuits() -> None:
    service = _build_service()
    character = _character()
    await _seed_schedule(
        service, character.id, date_=date(2026, 4, 18),
        activities=[_activity(9, 12, "morning")],
    )

    updated = await service.apply_adjustments(
        character_id=character.id,
        adjustments=[],
        date_=date(2026, 4, 18),
    )
    assert updated is None
