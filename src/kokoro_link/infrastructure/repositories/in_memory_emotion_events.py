"""In-process emotion-event store for dev / tests."""

from __future__ import annotations

from datetime import datetime

from kokoro_link.contracts.emotion import EmotionEventRepositoryPort
from kokoro_link.domain.entities.emotion_event import EmotionEvent


class InMemoryEmotionEventRepository(EmotionEventRepositoryPort):
    def __init__(self) -> None:
        self._rows: list[EmotionEvent] = []

    async def add(self, event: EmotionEvent) -> None:
        self._rows.append(event)

    async def add_many(self, events: list[EmotionEvent]) -> None:
        self._rows.extend(events)

    async def list_recent(
        self,
        *,
        character_id: str,
        operator_id: str,
        since: datetime,
        limit: int = 100,
    ) -> list[EmotionEvent]:
        matches = [
            r for r in self._rows
            if r.character_id == character_id
            and r.operator_id == operator_id
            and r.created_at >= since
        ]
        matches.sort(key=lambda r: r.created_at, reverse=True)
        return matches[:limit]

    async def delete_by_cause(
        self,
        *,
        character_id: str,
        cause_ref_kind: str,
        cause_ref_id: str,
    ) -> int:
        # Mirrors the SQL adapter exactly, including the refusal to act
        # on a partial key — this store is what the undo tests run
        # against, so a laxer rule here would test a repository that
        # does not exist in production.
        if not character_id or not cause_ref_kind or not cause_ref_id:
            return 0
        before = len(self._rows)
        self._rows = [
            r for r in self._rows
            if not (
                r.character_id == character_id
                and r.cause_ref_kind == cause_ref_kind
                and r.cause_ref_id == cause_ref_id
            )
        ]
        return before - len(self._rows)

    async def delete_for_character(self, character_id: str) -> int:
        before = len(self._rows)
        self._rows = [r for r in self._rows if r.character_id != character_id]
        return before - len(self._rows)
