"""KB2 — beat lineage survives every copy a schedule activity is made into.

``source_beat_id`` is the only thing standing between a planned scene and
a memory of an event the player never took part in. A mirror that drops
it (persistence, a turn-undo snapshot, a character backup) hands the
memorializer a block that looks ordinary, and the guard silently stops
holding — so each hop is pinned here.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from kokoro_link.application.dto.character_backup.registry import (
    BACKUP_TABLE_RULES_BY_NAME,
)
from kokoro_link.application.services.turn_snapshot_codec import (
    schedule_from_dict,
    schedule_to_dict,
)
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    ScheduleActivity,
)
from kokoro_link.infrastructure.persistence.sa_schedule_mapping import (
    _activity_to_row,
    _row_to_activity,
)

UTC = timezone.utc

_DAY = date(2026, 4, 18)
_BEAT_ID = "beat-silver-ring"


def _slot() -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, 14, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 18, 16, 0, tzinfo=UTC),
        description="後山林道那場戲的時段",
        category="story",
        source_beat_id=_BEAT_ID,
    )


def test_persistence_round_trip_keeps_the_lineage() -> None:
    row = _activity_to_row(_slot(), schedule_id="s1", position=0)

    assert row.source_beat_id == _BEAT_ID
    assert _row_to_activity(row).source_beat_id == _BEAT_ID


def test_a_legacy_row_without_the_column_reads_as_ordinary() -> None:
    """Rows written before the migration recorded no lineage; absence is
    "an ordinary activity", never a guess from the block's text."""
    row = _activity_to_row(_slot(), schedule_id="s1", position=0)
    del row.source_beat_id

    assert _row_to_activity(row).source_beat_id is None


def test_turn_snapshot_round_trip_keeps_the_lineage() -> None:
    schedule = DailySchedule.create(
        character_id="c1", date_=_DAY, activities=[_slot()],
    )

    restored = schedule_from_dict(schedule_to_dict(schedule))

    assert restored.activities[0].source_beat_id == _BEAT_ID


def test_a_snapshot_taken_before_the_field_restores_as_ordinary() -> None:
    schedule = DailySchedule.create(
        character_id="c1", date_=_DAY, activities=[_slot()],
    )
    payload = schedule_to_dict(schedule)
    payload["activities"][0].pop("source_beat_id")

    restored = schedule_from_dict(payload)

    assert restored.activities[0].source_beat_id is None


def test_the_backup_dto_carries_the_column() -> None:
    """Restoring a backup must not resurrect a reserved slot as an
    ordinary block — the registry's own parity test would catch a missing
    field, but this states *why* the field has to be there."""
    rule = BACKUP_TABLE_RULES_BY_NAME["schedule_activities"]

    assert "source_beat_id" in rule.dto.model_fields
