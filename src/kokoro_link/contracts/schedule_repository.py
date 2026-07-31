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

    async def claim_memorialize(self, activity_ids: list[str]) -> set[str]:
        """Atomically mark activities as ``memorialized`` (P3-Dedup §3.4).

        CAS ``UPDATE schedule_activities SET memorialized=true WHERE id IN
        (...) AND memorialized=false``; returns exactly the ids this caller
        flipped ``false → true``. Under a single runner every id is claimed
        (behaviour identical to marking after the write); under a distributed
        race only one runner claims a given activity and proceeds to write
        its episodic memory, so the visible memory is never double-written."""

    async def set_weather_vet(
        self,
        character_id: str,
        date_: date,
        *,
        activity_id: str | None,
        condition: str | None,
    ) -> None:
        """Stamp the intra-day weather-drift gate marker on (character, date).

        A targeted two-column update, deliberately NOT a whole-day ``save``:
        the vet's common outcome is "we asked and nothing contradicted the
        sky", and persisting that through ``save`` would replace the entire
        activity collection — reverting the memorialize CAS, an encounter
        block, or a chat-extracted commitment that landed while the judge was
        thinking. A missing day is a silent no-op (it was deleted or
        regenerated meanwhile; there is nothing left to gate)."""

    async def release_memorialize(self, activity_ids: list[str]) -> None:
        """Undo a claim: reset ``memorialized=false`` for ``activity_ids``.

        Called only when the memory write that a claim gated fails (embedder
        outage / persist error) so the activity stays eligible for the next
        pass — preserving the single-runner retry semantics exactly."""
