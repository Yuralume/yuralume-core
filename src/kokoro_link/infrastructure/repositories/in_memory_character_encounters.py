"""In-memory character encounter repository."""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.contracts.character_encounter import CharacterEncounterRepositoryPort
from kokoro_link.domain.entities.character_encounter import CharacterEncounter


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


class InMemoryCharacterEncounterRepository(CharacterEncounterRepositoryPort):
    def __init__(self) -> None:
        self._items: dict[str, CharacterEncounter] = {}

    async def get(self, encounter_id: str) -> CharacterEncounter | None:
        return self._items.get(encounter_id)

    async def save(self, encounter: CharacterEncounter) -> None:
        self._items[encounter.id] = encounter

    async def list_for_character(
        self, character_id: str, *, limit: int = 30,
    ) -> list[CharacterEncounter]:
        rows = [
            item for item in self._items.values()
            if item.character_a_id == character_id or item.character_b_id == character_id
        ]
        rows.sort(key=lambda item: item.scheduled_for, reverse=True)
        return rows[:limit]

    async def list_for_relationship(
        self, relationship_id: str, *, limit: int = 30,
    ) -> list[CharacterEncounter]:
        rows = [
            item for item in self._items.values()
            if item.relationship_id == relationship_id
        ]
        rows.sort(key=lambda item: item.scheduled_for, reverse=True)
        return rows[:limit]

    async def list_runnable(
        self, now: datetime, *, limit: int = 20,
    ) -> list[CharacterEncounter]:
        floor = _as_utc(now)
        rows = [
            item for item in self._items.values()
            if item.status == "planned" and item.scheduled_for <= floor
        ]
        rows.sort(key=lambda item: item.scheduled_for)
        return rows[:limit]

    async def count_for_character_since(self, character_id: str, since: datetime) -> int:
        floor = _as_utc(since)
        return sum(
            1 for item in self._items.values()
            if (
                item.character_a_id == character_id
                or item.character_b_id == character_id
            )
            and item.scheduled_for >= floor
            and item.status in {"planned", "running", "completed"}
        )

    async def has_pending_for_relationship(self, relationship_id: str) -> bool:
        return any(
            item.relationship_id == relationship_id
            and item.status in {"planned", "running"}
            for item in self._items.values()
        )

    async def delete_for_character(self, character_id: str) -> int:
        target = [
            item_id for item_id, item in self._items.items()
            if item.character_a_id == character_id or item.character_b_id == character_id
        ]
        for item_id in target:
            del self._items[item_id]
        return len(target)
