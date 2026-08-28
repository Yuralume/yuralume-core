"""KB2 — a reserved story-beat slot never becomes an episodic memory.

The incident this pins: a beat the player was central to got staged into
the character's day, the day completed, and the memorializer wrote it up
as something she had lived through — so she then spoke to the player
about a rescue he had never taken part in. The block now carries the
beat's id, and the memorializer skips it unconditionally: the beat's own
record belongs to the realize path, which is the single writer for it.

Preparation *for* a beat carries no lineage and is memorialised as usual
— the character really did spend that hour getting ready.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from kokoro_link.application.services.schedule_memorializer import (
    ScheduleMemorializer,
)
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    ScheduleActivity,
)
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)

UTC = timezone.utc

_DAY = date(2026, 4, 18)
_AFTER_THE_DAY = datetime(2026, 4, 18, 22, 0, tzinfo=UTC)
_BEAT_ID = "beat-silver-ring"


def _activity(
    start_h: int,
    end_h: int,
    description: str,
    *,
    source_beat_id: str | None = None,
) -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, start_h, 0, tzinfo=UTC),
        end_at=datetime(2026, 4, 18, end_h, 0, tzinfo=UTC),
        description=description,
        category="story" if source_beat_id else "prep",
        busy_score=0.6,
        source_beat_id=source_beat_id,
    )


async def _run(activities: list[ScheduleActivity]) -> tuple[
    int, list, DailySchedule,
]:
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    await schedule_repo.save(
        DailySchedule.create(
            character_id="c1", date_=_DAY, activities=activities,
        ),
    )
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
    )
    written = await memorializer.memorialize(
        character_id="c1", now=_AFTER_THE_DAY,
    )
    memories = await memory_repo.query("c1", limit=10)
    stored = await schedule_repo.get("c1", _DAY)
    assert stored is not None
    return written, memories, stored


@pytest.mark.asyncio
async def test_beat_slot_writes_no_memory_but_is_latched() -> None:
    written, memories, stored = await _run(
        [_activity(14, 16, "在後山林道救援", source_beat_id=_BEAT_ID)],
    )

    assert written == 0
    assert memories == []
    # Latched all the same, so every later pass skips it instead of
    # re-deciding — same contract as an encounter block.
    slot = stored.activities[0]
    assert slot.memorialized is True
    assert slot.has_memory is False


@pytest.mark.asyncio
async def test_preparation_beside_the_slot_is_still_memorialised() -> None:
    """The whole point of the lineage: it separates the planned scene
    from the real hours around it, instead of muting the whole day."""
    written, memories, stored = await _run(
        [
            _activity(9, 11, "整理裝備、確認路線"),
            _activity(14, 16, "在後山林道救援", source_beat_id=_BEAT_ID),
        ],
    )

    assert written == 1
    assert len(memories) == 1
    assert "整理裝備" in memories[0].content
    assert all("救援" not in m.content for m in memories)
    assert [a.memorialized for a in stored.activities] == [True, True]
    assert [a.has_memory for a in stored.activities] == [True, False]


@pytest.mark.asyncio
async def test_beat_status_is_never_consulted() -> None:
    """The skip is structural. A second run — the state a realized beat
    would be reached in — still writes nothing, so the realize path stays
    the only writer for that event."""
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    await schedule_repo.save(
        DailySchedule.create(
            character_id="c1",
            date_=_DAY,
            activities=[
                _activity(14, 16, "在後山林道救援", source_beat_id=_BEAT_ID),
            ],
        ),
    )
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
    )

    first = await memorializer.memorialize(character_id="c1", now=_AFTER_THE_DAY)
    second = await memorializer.memorialize(character_id="c1", now=_AFTER_THE_DAY)

    assert (first, second) == (0, 0)
    assert await memory_repo.query("c1", limit=10) == []
