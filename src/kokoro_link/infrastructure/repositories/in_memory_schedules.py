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

    async def set_weather_vet(
        self,
        character_id: str,
        date_: date,
        *,
        activity_id: str | None,
        condition: str | None,
    ) -> None:
        schedule = self._by_character.get(character_id, {}).get(date_)
        if schedule is None:
            return
        self._by_character[character_id][date_] = schedule.with_weather_vet(
            activity_id, condition,
        )

    async def claim_memorialize(self, activity_ids: list[str]) -> set[str]:
        wanted = set(activity_ids)
        if not wanted:
            return set()
        claimed: set[str] = set()
        for bucket in self._by_character.values():
            for date_, schedule in list(bucket.items()):
                updated = []
                changed = False
                for activity in schedule.activities:
                    if activity.id in wanted and not activity.memorialized:
                        updated.append(
                            activity.with_memory_state(
                                memorialized=True,
                                has_memory=activity.has_memory,
                            ),
                        )
                        claimed.add(activity.id)
                        changed = True
                    else:
                        updated.append(activity)
                if changed:
                    bucket[date_] = schedule.with_activities(updated)
        return claimed

    async def release_memorialize(self, activity_ids: list[str]) -> None:
        wanted = set(activity_ids)
        if not wanted:
            return
        for bucket in self._by_character.values():
            for date_, schedule in list(bucket.items()):
                updated = []
                changed = False
                for activity in schedule.activities:
                    if activity.id in wanted and activity.memorialized:
                        updated.append(
                            activity.with_memory_state(
                                memorialized=False,
                                has_memory=activity.has_memory,
                            ),
                        )
                        changed = True
                    else:
                        updated.append(activity)
                if changed:
                    bucket[date_] = schedule.with_activities(updated)
