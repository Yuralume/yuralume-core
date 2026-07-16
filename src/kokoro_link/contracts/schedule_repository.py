"""Schedule repository port."""

from __future__ import annotations

from datetime import date
from typing import Protocol

from kokoro_link.domain.entities.schedule import DailySchedule


class ScheduleRepositoryPort(Protocol):
    async def get(self, character_id: str, date_: date) -> DailySchedule | None:
        """Fetch the schedule for (character, civil date), if any."""

    async def save(self, schedule: DailySchedule) -> None:
        """Upsert a daily schedule."""

    async def list_for_character(
        self,
        character_id: str,
        *,
        limit: int = 30,
    ) -> list[DailySchedule]:
        """Return the most recent schedules for a character, newest first."""

    async def delete_for_character(self, character_id: str) -> int:
        """Cascade delete all schedules for a character."""
