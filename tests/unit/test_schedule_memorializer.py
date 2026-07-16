"""Tests for the ScheduleMemorializer — past activities → episodic memories."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from kokoro_link.application.services.schedule_memorializer import ScheduleMemorializer
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    OPERATOR_CONFIRMED_SHARED_ROLE,
    OPERATOR_INVITE_PENDING_ROLE,
    OPERATOR_WISH_ROLE,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)

UTC = timezone.utc


class _OperatorProfileService:
    async def get_for_user(self, user_id: str) -> OperatorProfile:
        return OperatorProfile(
            id=user_id,
            display_name=user_id,
            timezone_id="Asia/Taipei",
        )


def _make_activity(
    day: date,
    start_h: int,
    end_h: int,
    description: str,
    *,
    category: str = "work",
    busy: float = 0.8,
    memorialized: bool = False,
) -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(day.year, day.month, day.day, start_h, 0, tzinfo=UTC),
        end_at=datetime(day.year, day.month, day.day, end_h, 0, tzinfo=UTC),
        description=description,
        category=category,
        busy_score=busy,
        memorialized=memorialized,
    )


@pytest.mark.asyncio
async def test_memorializes_completed_activities() -> None:
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
    )
    today = date(2026, 4, 18)
    schedule = DailySchedule.create(
        character_id="c1",
        date_=today,
        activities=[
            _make_activity(today, 9, 12, "在工作室剪輯"),
            _make_activity(today, 14, 18, "開會"),
        ],
    )
    await schedule_repo.save(schedule)

    now = datetime(2026, 4, 18, 20, 0, tzinfo=UTC)
    count = await memorializer.memorialize(character_id="c1", now=now)
    assert count == 2

    memories = await memory_repo.query("c1", limit=10)
    assert len(memories) == 2
    assert all(m.kind == MemoryKind.EPISODIC for m in memories)
    assert any("剪輯" in m.content for m in memories)


@pytest.mark.asyncio
async def test_skips_incomplete_activities() -> None:
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
    )
    today = date(2026, 4, 18)
    schedule = DailySchedule.create(
        character_id="c1",
        date_=today,
        activities=[
            _make_activity(today, 9, 11, "早晨會議"),
            _make_activity(today, 14, 18, "下午工作"),
        ],
    )
    await schedule_repo.save(schedule)

    # Only the morning one has ended
    now = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    count = await memorializer.memorialize(character_id="c1", now=now)
    assert count == 1

    memories = await memory_repo.query("c1", limit=10)
    assert len(memories) == 1
    assert "早晨會議" in memories[0].content


@pytest.mark.asyncio
async def test_idempotent_on_repeated_runs() -> None:
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
    )
    today = date(2026, 4, 18)
    schedule = DailySchedule.create(
        character_id="c1",
        date_=today,
        activities=[_make_activity(today, 9, 12, "工作")],
    )
    await schedule_repo.save(schedule)

    now = datetime(2026, 4, 18, 20, 0, tzinfo=UTC)
    first = await memorializer.memorialize(character_id="c1", now=now)
    second = await memorializer.memorialize(character_id="c1", now=now)

    assert first == 1
    assert second == 0

    memories = await memory_repo.query("c1", limit=10)
    assert len(memories) == 1


@pytest.mark.asyncio
async def test_memorialized_flag_persists() -> None:
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
    )
    today = date(2026, 4, 18)
    schedule = DailySchedule.create(
        character_id="c1",
        date_=today,
        activities=[_make_activity(today, 9, 12, "工作")],
    )
    await schedule_repo.save(schedule)

    now = datetime(2026, 4, 18, 20, 0, tzinfo=UTC)
    await memorializer.memorialize(character_id="c1", now=now)

    reloaded = await schedule_repo.get("c1", today)
    assert reloaded is not None
    assert reloaded.activities[0].memorialized is True
    assert reloaded.activities[0].has_memory is True


@pytest.mark.asyncio
async def test_encounter_partner_activity_is_marked_without_generic_memory() -> None:
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
    )
    today = date(2026, 4, 18)
    encounter_activity = ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 18, 10, 0, tzinfo=UTC),
        description="與B短暫碰面",
        category="social",
        busy_score=0.25,
        participant_refs=(
            ParticipantRef(
                actor_kind="character",
                actor_id="c2",
                display_name="B",
                role="encounter_partner",
            ),
        ),
    )
    normal_activity = _make_activity(today, 11, 12, "整理筆記")
    await schedule_repo.save(
        DailySchedule.create(
            character_id="c1",
            date_=today,
            activities=[encounter_activity, normal_activity],
        ),
    )

    count = await memorializer.memorialize(
        character_id="c1",
        now=datetime(2026, 4, 18, 20, 0, tzinfo=UTC),
    )

    assert count == 1
    memories = await memory_repo.query("c1", limit=10)
    assert len(memories) == 1
    assert "整理筆記" in memories[0].content
    assert "與B短暫碰面" not in memories[0].content
    reloaded = await schedule_repo.get("c1", today)
    assert reloaded is not None
    assert [activity.memorialized for activity in reloaded.activities] == [True, True]
    assert [activity.has_memory for activity in reloaded.activities] == [False, True]


@pytest.mark.asyncio
async def test_deduped_activity_is_marked_as_having_memory() -> None:
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
    )
    today = date(2026, 4, 18)
    activity = _make_activity(today, 9, 10, "在工作室剪輯")
    await schedule_repo.save(
        DailySchedule.create(character_id="c1", date_=today, activities=[activity]),
    )
    await memory_repo.add_many([
        MemoryItem.create(
            character_id="c1",
            kind=MemoryKind.EPISODIC,
            content="在工作室剪輯（2026-04-18 六 09:00-10:00）",
            tags=("schedule", "work"),
        ),
    ])

    count = await memorializer.memorialize(
        character_id="c1",
        now=datetime(2026, 4, 18, 12, 0, tzinfo=UTC),
    )

    assert count == 0
    reloaded = await schedule_repo.get("c1", today)
    assert reloaded is not None
    assert reloaded.activities[0].memorialized is True
    assert reloaded.activities[0].has_memory is True


@pytest.mark.asyncio
async def test_schedule_memory_content_puts_semantic_activity_before_time() -> None:
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
    )
    today = date(2026, 4, 18)
    await schedule_repo.save(
        DailySchedule.create(
            character_id="c1",
            date_=today,
            activities=[
                ScheduleActivity.create(
                    start_at=datetime(2026, 4, 18, 9, 0, tzinfo=UTC),
                    end_at=datetime(2026, 4, 18, 10, 0, tzinfo=UTC),
                    description="寫劇本大綱",
                    category="creative",
                    location="咖啡店",
                ),
            ],
        ),
    )

    await memorializer.memorialize(
        character_id="c1",
        now=datetime(2026, 4, 18, 12, 0, tzinfo=UTC),
    )

    memories = await memory_repo.query("c1", limit=10)
    assert len(memories) == 1
    assert memories[0].content.startswith("在咖啡店寫劇本大綱")
    assert "2026-04-18" in memories[0].content
    assert "09:00-10:00" in memories[0].content


@pytest.mark.asyncio
async def test_operator_pending_or_wish_activity_does_not_write_shared_memory() -> None:
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
    )
    today = date(2026, 4, 18)
    pending = ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 18, 10, 0, tzinfo=UTC),
        description="想約你看電影",
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
    wish = ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, 11, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 18, 12, 0, tzinfo=UTC),
        description="想著下次可以聊電影",
        category="social",
        participant_refs=(
            ParticipantRef(
                actor_kind="operator",
                actor_id=None,
                display_name="使用者",
                role=OPERATOR_WISH_ROLE,
            ),
        ),
    )
    await schedule_repo.save(
        DailySchedule.create(character_id="c1", date_=today, activities=[pending, wish]),
    )

    count = await memorializer.memorialize(
        character_id="c1",
        now=datetime(2026, 4, 18, 13, 0, tzinfo=UTC),
    )

    assert count == 0
    assert await memory_repo.query("c1", limit=10) == []
    reloaded = await schedule_repo.get("c1", today)
    assert reloaded is not None
    assert [activity.memorialized for activity in reloaded.activities] == [True, True]
    assert [activity.has_memory for activity in reloaded.activities] == [False, False]


@pytest.mark.asyncio
async def test_operator_confirmed_shared_activity_can_be_memorialized() -> None:
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
    )
    today = date(2026, 4, 18)
    activity = ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, 9, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 18, 10, 0, tzinfo=UTC),
        description="一起看電影",
        category="social",
        participant_refs=(
            ParticipantRef(
                actor_kind="operator",
                actor_id=None,
                display_name="使用者",
                role=OPERATOR_CONFIRMED_SHARED_ROLE,
            ),
        ),
    )
    await schedule_repo.save(
        DailySchedule.create(character_id="c1", date_=today, activities=[activity]),
    )

    count = await memorializer.memorialize(
        character_id="c1",
        now=datetime(2026, 4, 18, 13, 0, tzinfo=UTC),
    )

    assert count == 1
    memories = await memory_repo.query("c1", limit=10)
    assert len(memories) == 1
    assert memories[0].participants[0].role == OPERATOR_CONFIRMED_SHARED_ROLE
    reloaded = await schedule_repo.get("c1", today)
    assert reloaded is not None
    assert reloaded.activities[0].has_memory is True


@pytest.mark.asyncio
async def test_processes_yesterday_and_today() -> None:
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
    )
    today = date(2026, 4, 18)
    yesterday = today - timedelta(days=1)
    await schedule_repo.save(
        DailySchedule.create(
            character_id="c1", date_=yesterday,
            activities=[_make_activity(yesterday, 14, 17, "昨日工作")],
        )
    )
    await schedule_repo.save(
        DailySchedule.create(
            character_id="c1", date_=today,
            activities=[_make_activity(today, 9, 11, "今早會議")],
        )
    )

    now = datetime(2026, 4, 18, 12, 0, tzinfo=UTC)
    count = await memorializer.memorialize(character_id="c1", now=now)
    assert count == 2

    contents = {m.content for m in await memory_repo.query("c1", limit=10)}
    assert any("昨日工作" in c for c in contents)
    assert any("今早會議" in c for c in contents)


@pytest.mark.asyncio
async def test_empty_when_no_schedule() -> None:
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
    )
    count = await memorializer.memorialize(
        character_id="missing", now=datetime(2026, 4, 18, 12, 0, tzinfo=UTC),
    )
    assert count == 0


@pytest.mark.asyncio
async def test_uses_owner_timezone_for_scan_targets_and_memory_text() -> None:
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    character_repo = InMemoryCharacterRepository()
    character = Character.create(
        name="Airi",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=80,
        ),
    )
    character = replace(character, user_id="owner-tw")
    await character_repo.save(character)
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
        character_repository=character_repo,
        operator_profile_service=_OperatorProfileService(),
    )
    owner_tz = ZoneInfo("Asia/Taipei")
    owner_day = date(2026, 6, 15)
    activity = ScheduleActivity.create(
        start_at=datetime(2026, 6, 15, 0, 0, tzinfo=owner_tz),
        end_at=datetime(2026, 6, 15, 0, 30, tzinfo=owner_tz),
        description="整理午夜後的筆記",
        category="work",
        busy_score=0.7,
    )
    await schedule_repo.save(DailySchedule.create(
        character_id=character.id,
        date_=owner_day,
        activities=[activity],
    ))

    count = await memorializer.memorialize(
        character_id=character.id,
        now=datetime(2026, 6, 14, 16, 45, tzinfo=UTC),
    )

    assert count == 1
    memories = await memory_repo.query(character.id, limit=10)
    assert len(memories) == 1
    assert "2026-06-15" in memories[0].content
    assert "00:00-00:30" in memories[0].content
