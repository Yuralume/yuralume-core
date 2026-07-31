"""Persistence parity for the schedule weather-drift vet marker.

The intra-day drift gate is only idempotent across ticks and across
instances if the marker survives a save→get round trip in **both** repo
adapters. The SA side is exercised through its mapping functions (the
same ones the repository calls) so this stays a unit test; the DB-level
round trip lives in ``tests/integration/test_sa_repositories.py``.
"""

from __future__ import annotations

from datetime import date as date_cls, datetime, timezone
from types import SimpleNamespace

import pytest

from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.infrastructure.persistence.sa_schedule_mapping import (
    apply_schedule_to_row,
    row_to_schedule,
    schedule_to_row,
)
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)


TARGET_DATE = date_cls(2026, 4, 18)


def _activity(description: str = "午後散步") -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, 14, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 4, 18, 15, 0, tzinfo=timezone.utc),
        description=description,
        category="leisure",
        location="河堤",
    )


def _schedule(**vet: str) -> DailySchedule:
    schedule = DailySchedule.create(
        character_id="char-A", date_=TARGET_DATE, activities=[_activity()],
    )
    if vet:
        return schedule.with_weather_vet(
            vet["activity_id"], vet["condition"],
        )
    return schedule


class TestSAMappingCarriesTheVetMarker:
    def test_new_row_round_trips_the_marker(self) -> None:
        schedule = _schedule(activity_id="act-1", condition="小雨")
        restored = row_to_schedule(schedule_to_row(schedule))
        assert restored.weather_vet_activity_id == "act-1"
        assert restored.weather_vet_condition == "小雨"

    def test_unvetted_day_round_trips_as_none(self) -> None:
        restored = row_to_schedule(schedule_to_row(_schedule()))
        assert restored.weather_vet_activity_id is None
        assert restored.weather_vet_condition is None

    def test_apply_to_existing_row_updates_the_marker(self) -> None:
        row = schedule_to_row(_schedule(activity_id="act-1", condition="小雨"))
        # Same day, same activity, weather turned — the marker must move so
        # the gate re-opens instead of skipping forever.
        apply_schedule_to_row(_schedule(activity_id="act-1", condition="晴朗"), row)
        assert row_to_schedule(row).weather_vet_condition == "晴朗"

    def test_apply_to_existing_row_can_clear_the_marker(self) -> None:
        row = schedule_to_row(_schedule(activity_id="act-1", condition="小雨"))
        apply_schedule_to_row(_schedule(), row)
        restored = row_to_schedule(row)
        assert restored.weather_vet_activity_id is None
        assert restored.weather_vet_condition is None

    def test_legacy_row_without_the_columns_reads_as_unvetted(self) -> None:
        # A row shape predating the migration carries no such attributes; the
        # mapping must degrade to "never vetted" rather than raise, the same
        # way it already tolerates a missing ``is_planned``.
        legacy = SimpleNamespace(
            id="sched-legacy",
            character_id="char-A",
            date=TARGET_DATE.isoformat(),
            generated_at=datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc),
            activities=[],
        )
        restored = row_to_schedule(legacy)
        assert restored.weather_vet_activity_id is None
        assert restored.weather_vet_condition is None


class TestInMemoryRepositoryCarriesTheVetMarker:
    @pytest.mark.asyncio
    async def test_save_get_round_trips_the_marker(self) -> None:
        repo = InMemoryScheduleRepository()
        await repo.save(_schedule(activity_id="act-1", condition="雷雨"))

        loaded = await repo.get("char-A", TARGET_DATE)
        assert loaded is not None
        assert loaded.weather_vet_activity_id == "act-1"
        assert loaded.weather_vet_condition == "雷雨"

    @pytest.mark.asyncio
    async def test_unvetted_day_round_trips_as_none(self) -> None:
        repo = InMemoryScheduleRepository()
        await repo.save(_schedule())

        loaded = await repo.get("char-A", TARGET_DATE)
        assert loaded is not None
        assert loaded.weather_vet_activity_id is None
        assert loaded.weather_vet_condition is None

    @pytest.mark.asyncio
    async def test_set_weather_vet_touches_only_the_marker(self) -> None:
        # The vet's common outcome is "asked, nothing contradicted". Persisting
        # that through a whole-day save would replace the activity collection
        # and revert whatever else landed meanwhile, so the marker gets its own
        # targeted write.
        repo = InMemoryScheduleRepository()
        schedule = _schedule()
        await repo.save(schedule)

        await repo.set_weather_vet(
            "char-A", TARGET_DATE, activity_id="act-1", condition="大雨",
        )

        loaded = await repo.get("char-A", TARGET_DATE)
        assert loaded is not None
        assert loaded.weather_vet_activity_id == "act-1"
        assert loaded.weather_vet_condition == "大雨"
        assert [a.description for a in loaded.activities] == ["午後散步"]

    @pytest.mark.asyncio
    async def test_set_weather_vet_on_a_missing_day_is_a_no_op(self) -> None:
        repo = InMemoryScheduleRepository()
        await repo.set_weather_vet(
            "char-A", TARGET_DATE, activity_id="act-1", condition="大雨",
        )
        assert await repo.get("char-A", TARGET_DATE) is None

    @pytest.mark.asyncio
    async def test_memorialize_claim_preserves_the_marker(self) -> None:
        # claim_memorialize rebuilds the day via ``with_activities``; the
        # marker must not be dropped by that rewrite path.
        repo = InMemoryScheduleRepository()
        schedule = _schedule(activity_id="act-1", condition="陰天")
        await repo.save(schedule)

        await repo.claim_memorialize([schedule.activities[0].id])

        loaded = await repo.get("char-A", TARGET_DATE)
        assert loaded is not None
        assert loaded.activities[0].memorialized is True
        assert loaded.weather_vet_activity_id == "act-1"
        assert loaded.weather_vet_condition == "陰天"
