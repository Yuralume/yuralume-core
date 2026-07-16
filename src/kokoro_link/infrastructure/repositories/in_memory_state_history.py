"""In-process ``StateHistoryRepositoryPort`` implementation."""

from collections import defaultdict
from datetime import datetime, timezone

from kokoro_link.contracts.state_history import StateHistoryRepositoryPort
from kokoro_link.domain.entities.state_snapshot import StateSnapshot


class InMemoryStateHistoryRepository(StateHistoryRepositoryPort):
    def __init__(self) -> None:
        self._by_character: dict[str, list[StateSnapshot]] = defaultdict(list)

    async def add(self, snapshot: StateSnapshot) -> None:
        self._by_character[snapshot.character_id].append(snapshot)

    async def query(
        self,
        character_id: str,
        *,
        limit: int = 50,
    ) -> list[StateSnapshot]:
        items = self._by_character.get(character_id, [])
        sorted_items = sorted(items, key=lambda s: s.created_at, reverse=True)
        return sorted_items[:limit]

    async def delete_many(self, snapshot_ids: list[str]) -> int:
        if not snapshot_ids:
            return 0
        victims = set(snapshot_ids)
        removed = 0
        for character_id, items in list(self._by_character.items()):
            kept = [s for s in items if s.id not in victims]
            removed += len(items) - len(kept)
            if kept:
                self._by_character[character_id] = kept
            else:
                self._by_character.pop(character_id, None)
        return removed

    async def delete_created_since(
        self, character_id: str, since: datetime,
    ) -> int:
        items = self._by_character.get(character_id, [])
        if not items:
            return 0
        floor = since if since.tzinfo else since.replace(tzinfo=timezone.utc)
        kept: list[StateSnapshot] = []
        removed = 0
        for snap in items:
            created = snap.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created >= floor:
                removed += 1
            else:
                kept.append(snap)
        if kept:
            self._by_character[character_id] = kept
        else:
            self._by_character.pop(character_id, None)
        return removed

    async def delete_for_character(self, character_id: str) -> int:
        removed = self._by_character.pop(character_id, None)
        return len(removed) if removed else 0
