"""In-memory character encounter intent repository."""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.contracts.character_encounter_intent import (
    CharacterEncounterIntentRepositoryPort,
)
from kokoro_link.domain.entities.character_encounter_intent import (
    CharacterEncounterIntent,
)


def _as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _matches_pair(
    intent: CharacterEncounterIntent,
    character_a_id: str,
    character_b_id: str,
) -> bool:
    return {
        intent.character_id,
        intent.peer_character_id,
    } == {character_a_id, character_b_id}


class InMemoryCharacterEncounterIntentRepository(
    CharacterEncounterIntentRepositoryPort,
):
    def __init__(self) -> None:
        self._items: dict[str, CharacterEncounterIntent] = {}

    async def get(self, intent_id: str) -> CharacterEncounterIntent | None:
        return self._items.get(intent_id)

    async def add(self, intent: CharacterEncounterIntent) -> None:
        self._items[intent.id] = intent

    async def save(self, intent: CharacterEncounterIntent) -> None:
        self._items[intent.id] = intent

    async def find_pending_for_pair(
        self,
        character_a_id: str,
        character_b_id: str,
        *,
        now: datetime,
        horizon: datetime,
    ) -> CharacterEncounterIntent | None:
        moment = _as_utc(now)
        ceiling = _as_utc(horizon)
        rows = [
            item for item in self._items.values()
            if _matches_pair(item, character_a_id, character_b_id)
            and item.status == "pending"
            and item.desired_after <= ceiling
            and item.expires_at is not None
            and item.expires_at > moment
        ]
        rows.sort(key=lambda item: (item.desired_after, item.created_at))
        return rows[0] if rows else None

    async def list_pending_for_character(
        self, character_id: str, *, now: datetime, limit: int = 30,
    ) -> list[CharacterEncounterIntent]:
        moment = _as_utc(now)
        rows = [
            item for item in self._items.values()
            if (
                item.character_id == character_id
                or item.peer_character_id == character_id
            )
            and item.status == "pending"
            and item.expires_at is not None
            and item.expires_at > moment
        ]
        rows.sort(key=lambda item: (item.desired_after, item.created_at))
        return rows[:limit]

    async def delete_for_character(self, character_id: str) -> int:
        target = [
            item_id for item_id, item in self._items.items()
            if item.character_id == character_id or item.peer_character_id == character_id
        ]
        for item_id in target:
            del self._items[item_id]
        return len(target)
