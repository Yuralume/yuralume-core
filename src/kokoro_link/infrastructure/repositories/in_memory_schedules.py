"""In-process schedule repository for dev/tests."""

from __future__ import annotations

from collections import defaultdict
from datetime import date

from kokoro_link.contracts.schedule_repository import ScheduleRepositoryPort
from kokoro_link.domain.entities.schedule import DailySchedule


class InMemoryScheduleRepository(ScheduleRepositoryPort):
    def __init__(self) -> None:
        self._by_character: dict[str, dict[date, DailySchedule]] = defaultdict(dict)

    async def get(self, character_id: str, date_: date) -> DailySchedule | None:
        return self._by_character.get(character_id, {}).get(date_)

    async def save(self, schedule: DailySchedule) -> None:
        self._by_character[schedule.character_id][schedule.date] = schedule

    async def list_for_character(
        self,
        character_id: str,
        *,
        limit: int = 30,
    ) -> list[DailySchedule]:
        bucket = self._by_character.get(character_id, {})
        ordered = sorted(bucket.values(), key=lambda s: s.date, reverse=True)
        return ordered[:limit]

    async def delete_for_character(self, character_id: str) -> int:
        bucket = self._by_character.pop(character_id, None)
        return len(bucket) if bucket else 0
