"""Persistence parity for the ``manually_adjusted`` operator-ownership flag.

The flag is what stops :meth:`ScheduleService._is_stale_current_day_plan`
from rebuilding a day the operator has edited (the「刪掉的行程自己補回來」
bug). It is only load-bearing if it survives a save→get round trip in the
SA mapping layer AND the turn-undo snapshot codec — a flag silently dropped
by either seam re-opens the resurrection path.
"""

from __future__ import annotations

from datetime import date as date_cls, datetime, timezone
from types import SimpleNamespace

from kokoro_link.application.services.turn_snapshot_codec import (
    schedule_from_dict,
    schedule_to_dict,
)
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.infrastructure.persistence.sa_schedule_mapping import (
    apply_schedule_to_row,
    row_to_schedule,
    schedule_to_row,
)


TARGET_DATE = date_cls(2026, 4, 18)


def _activity() -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, 14, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 4, 18, 15, 0, tzinfo=timezone.utc),
        description="午後散步",
        category="leisure",
    )


def _schedule(*, manual: bool = False) -> DailySchedule:
    schedule = DailySchedule.create(
        character_id="char-A", date_=TARGET_DATE, activities=[_activity()],
    )
    if manual:
        return schedule.with_manual_adjustment()
    return schedule


class TestDomainDefaults:
    def test_flag_defaults_to_false(self) -> None:
        assert _schedule().manually_adjusted is False

    def test_with_manual_adjustment_sets_and_clears(self) -> None:
        stamped = _schedule().with_manual_adjustment()
        assert stamped.manually_adjusted is True
        assert stamped.with_manual_adjustment(False).manually_adjusted is False

    def test_with_activities_preserves_the_flag(self) -> None:
        stamped = _schedule(manual=True)
        assert stamped.with_activities([]).manually_adjusted is True


class TestSAMappingCarriesTheFlag:
    def test_new_row_round_trips_true(self) -> None:
        restored = row_to_schedule(schedule_to_row(_schedule(manual=True)))
        assert restored.manually_adjusted is True

    def test_new_row_round_trips_false(self) -> None:
        restored = row_to_schedule(schedule_to_row(_schedule()))
        assert restored.manually_adjusted is False

    def test_apply_to_existing_row_updates_the_flag(self) -> None:
        row = schedule_to_row(_schedule())
        apply_schedule_to_row(_schedule(manual=True), row)
        assert row_to_schedule(row).manually_adjusted is True

    def test_legacy_row_without_the_column_reads_as_untouched(self) -> None:
        # A row shape predating the migration carries no such attribute;
        # the mapping must degrade to "never manually adjusted" — the
        # historical behaviour — rather than raise.
        legacy = SimpleNamespace(
            id="row-1",
            character_id="char-A",
            date=TARGET_DATE.isoformat(),
            generated_at=datetime(2026, 4, 18, 0, 0, tzinfo=timezone.utc),
            activities=[],
        )
        assert row_to_schedule(legacy).manually_adjusted is False


class TestSnapshotCodecCarriesTheFlag:
    def test_round_trips_true(self) -> None:
        restored = schedule_from_dict(schedule_to_dict(_schedule(manual=True)))
        assert restored.manually_adjusted is True

    def test_legacy_payload_without_the_key_reads_as_untouched(self) -> None:
        payload = schedule_to_dict(_schedule())
        payload.pop("manually_adjusted", None)
        assert schedule_from_dict(payload).manually_adjusted is False
