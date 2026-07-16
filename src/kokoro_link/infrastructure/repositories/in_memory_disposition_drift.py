"""In-process disposition-drift audit store (HUMANIZATION_ROADMAP §3.1)."""

from __future__ import annotations

from kokoro_link.contracts.disposition_drift import (
    DispositionDriftHistoryRepositoryPort,
)
from kokoro_link.domain.entities.disposition_drift_record import (
    DispositionDriftRecord,
)


class InMemoryDispositionDriftHistoryRepository(
    DispositionDriftHistoryRepositoryPort,
):
    def __init__(self) -> None:
        self._rows: list[DispositionDriftRecord] = []

    async def add(
        self, record: DispositionDriftRecord,
    ) -> DispositionDriftRecord:
        self._rows.append(record)
        return record

    async def list_for_character(
        self,
        character_id: str,
        *,
        limit: int = 20,
    ) -> list[DispositionDriftRecord]:
        matches = [r for r in self._rows if r.character_id == character_id]
        matches.sort(key=lambda r: r.decided_at, reverse=True)
        return matches[: max(1, limit)]

    async def latest_for_dimension(
        self,
        character_id: str,
        dimension: str,
    ) -> DispositionDriftRecord | None:
        matches = [
            r for r in self._rows
            if r.character_id == character_id and r.dimension == dimension
        ]
        if not matches:
            return None
        matches.sort(key=lambda r: r.decided_at, reverse=True)
        return matches[0]

    async def delete_for_character(self, character_id: str) -> int:
        before = len(self._rows)
        self._rows = [r for r in self._rows if r.character_id != character_id]
        return before - len(self._rows)
