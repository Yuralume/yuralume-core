"""Tests for the activity-aftermath integration in ScheduleMemorializer.

The base memorialiser writes a dry "what happened" memory. With an
``ActivityAftermathPort`` plugged in, it asks an LLM to read the
persona + activity + companions and produce a short emotional residue
("早上被大媽追問感情很煩躁"), which gets folded into the memory content
and tagged ``aftermath`` so the prompt builder can surface fresh ones
prominently next chat.

Per the project's top directive — the port is fully LLM-driven and we
test the *contract* (residue goes in, gets persisted) rather than
specific keyword logic. The fake adapter just echoes whatever residue
the test stages, which is enough to verify the wiring.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from kokoro_link.application.services.schedule_memorializer import (
    ScheduleMemorializer,
)
from kokoro_link.contracts.activity_aftermath import (
    ActivityAftermath,
    ActivityAftermathPort,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)


UTC = timezone.utc


def _character() -> Character:
    return Character.create(
        name="Airi",
        summary="高中二年級的甜點愛好者",
        personality=["怕生", "天然"],
        interests=["甜點", "看少女漫畫"],
        speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=50, fatigue=20, trust=50, energy=80,
        ),
    )


def _activity(
    day: date, start_h: int, end_h: int, description: str,
    *, busy: float = 0.7, companions: tuple[str, ...] = (),
) -> ScheduleActivity:
    return ScheduleActivity.create(
        start_at=datetime(day.year, day.month, day.day, start_h, 0, tzinfo=UTC),
        end_at=datetime(day.year, day.month, day.day, end_h, 0, tzinfo=UTC),
        description=description,
        category="社交",
        busy_score=busy,
        companion_names=companions,
    )


class _StubAftermathPort:
    """Test stub that returns a pre-staged aftermath per activity description.

    Lets a test say "when activity X completes, residue Y comes back" so
    we can assert the residue ends up in the persisted memory. Calls are
    counted for assertions about how often the port runs.
    """

    def __init__(self, mapping: dict[str, ActivityAftermath]) -> None:
        self._mapping = mapping
        self.calls: list[str] = []

    async def judge(
        self,
        *,
        character: Character,
        activity: ScheduleActivity,
        operator_primary_language: str = "zh-TW",
    ) -> ActivityAftermath:
        _ = operator_primary_language
        self.calls.append(activity.description)
        return self._mapping.get(activity.description, ActivityAftermath())


class _RaisingAftermathPort:
    """Test stub that raises — used to verify the memorialiser is fail-soft."""

    async def judge(
        self,
        *,
        character: Character,
        activity: ScheduleActivity,
        operator_primary_language: str = "zh-TW",
    ) -> ActivityAftermath:
        raise RuntimeError("aftermath LLM exploded")


async def _setup(
    aftermath_port: ActivityAftermathPort | None,
) -> tuple[
    ScheduleMemorializer,
    InMemoryScheduleRepository,
    InMemoryMemoryRepository,
    InMemoryCharacterRepository,
    Character,
]:
    schedule_repo = InMemoryScheduleRepository()
    memory_repo = InMemoryMemoryRepository()
    character_repo = InMemoryCharacterRepository()
    character = _character()
    await character_repo.save(character)
    memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repo,
        memory_repository=memory_repo,
        local_tz=UTC,
        aftermath_port=aftermath_port,
        character_repository=character_repo,
    )
    return memorializer, schedule_repo, memory_repo, character_repo, character


@pytest.mark.asyncio
async def test_aftermath_residue_is_appended_to_memory_content() -> None:
    """When the aftermath port returns a residue, the persisted memory
    must contain that residue text so the next chat's memory recall
    picks it up naturally."""
    stub = _StubAftermathPort({
        "和鄰居大媽聊天": ActivityAftermath(
            residue_summary="被一直追問感情狀況，很煩躁",
            emotion_tag="煩躁",
        ),
    })
    mem, sched_repo, memory_repo, _, character = await _setup(stub)

    today = date(2026, 5, 15)
    schedule = DailySchedule.create(
        character_id=character.id,
        date_=today,
        activities=[_activity(today, 9, 10, "和鄰居大媽聊天", companions=("大媽",))],
    )
    await sched_repo.save(schedule)

    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    count = await mem.memorialize(character_id=character.id, now=now)
    assert count == 1

    memories = await memory_repo.query(character.id, limit=10)
    assert len(memories) == 1
    assert "和鄰居大媽聊天" in memories[0].content  # bare description preserved
    assert "追問感情狀況" in memories[0].content   # residue folded in
    assert "aftermath" in memories[0].tags         # tagged for prompt promotion
    assert "煩躁" in memories[0].tags              # emotion tag stored as tag


@pytest.mark.asyncio
async def test_empty_aftermath_keeps_bare_memory() -> None:
    """A blank aftermath result must leave the memory at its bare-activity
    form (no residue text, no aftermath tag) — equivalent to legacy
    behaviour, so a low-signal activity doesn't fabricate a feeling."""
    stub = _StubAftermathPort({"刷牙": ActivityAftermath()})  # blank result
    mem, sched_repo, memory_repo, _, character = await _setup(stub)

    today = date(2026, 5, 15)
    schedule = DailySchedule.create(
        character_id=character.id,
        date_=today,
        activities=[_activity(today, 7, 8, "刷牙", busy=0.1)],
    )
    await sched_repo.save(schedule)

    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    await mem.memorialize(character_id=character.id, now=now)

    memories = await memory_repo.query(character.id, limit=10)
    assert len(memories) == 1
    assert "aftermath" not in memories[0].tags


@pytest.mark.asyncio
async def test_aftermath_port_failure_is_fail_soft() -> None:
    """If the aftermath port raises, the memorialiser must still write
    the bare-activity memory so a flaky LLM doesn't lose the schedule
    history. The activity should still be marked memorialised."""
    mem, sched_repo, memory_repo, _, character = await _setup(_RaisingAftermathPort())

    today = date(2026, 5, 15)
    schedule = DailySchedule.create(
        character_id=character.id,
        date_=today,
        activities=[_activity(today, 9, 10, "開會")],
    )
    await sched_repo.save(schedule)

    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    count = await mem.memorialize(character_id=character.id, now=now)
    assert count == 1

    memories = await memory_repo.query(character.id, limit=10)
    assert len(memories) == 1
    assert "開會" in memories[0].content
    # tags should not contain aftermath since residue computation failed
    assert "aftermath" not in memories[0].tags


@pytest.mark.asyncio
async def test_aftermath_port_absent_keeps_legacy_behaviour() -> None:
    """Older container wiring may omit the aftermath port entirely. The
    memorialiser must still function with the bare-activity memory."""
    mem, sched_repo, memory_repo, _, character = await _setup(None)

    today = date(2026, 5, 15)
    schedule = DailySchedule.create(
        character_id=character.id,
        date_=today,
        activities=[_activity(today, 9, 10, "晨跑")],
    )
    await sched_repo.save(schedule)

    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    count = await mem.memorialize(character_id=character.id, now=now)
    assert count == 1
    memories = await memory_repo.query(character.id, limit=10)
    assert "aftermath" not in memories[0].tags
    assert "晨跑" in memories[0].content


@pytest.mark.asyncio
async def test_aftermath_called_once_per_activity_not_per_run() -> None:
    """Two activities → two calls. Activities that were memorialised on a
    previous run must not be re-judged (would waste tokens)."""
    stub = _StubAftermathPort({
        "晨跑": ActivityAftermath(residue_summary="精神超好", emotion_tag="雀躍"),
        "開會": ActivityAftermath(residue_summary="會被同事煩到頭痛", emotion_tag="疲憊"),
    })
    mem, sched_repo, memory_repo, _, character = await _setup(stub)

    today = date(2026, 5, 15)
    schedule = DailySchedule.create(
        character_id=character.id,
        date_=today,
        activities=[
            _activity(today, 7, 8, "晨跑"),
            _activity(today, 9, 11, "開會"),
        ],
    )
    await sched_repo.save(schedule)

    now = datetime(2026, 5, 15, 12, 0, tzinfo=UTC)
    await mem.memorialize(character_id=character.id, now=now)
    assert len(stub.calls) == 2

    # Run again — both activities already memorialised, port shouldn't fire
    await mem.memorialize(character_id=character.id, now=now)
    assert len(stub.calls) == 2  # unchanged
