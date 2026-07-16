"""In-memory persona curiosity ledger for tests / local dev."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from kokoro_link.contracts.persona_curiosity import (
    PersonaCuriosityRepositoryPort,
)
from kokoro_link.domain.entities.persona_curiosity import (
    PersonaCuriosityAttempt,
)


class InMemoryPersonaCuriosityRepository(PersonaCuriosityRepositoryPort):
    def __init__(self) -> None:
        self._rows: dict[str, PersonaCuriosityAttempt] = {}

    async def add(
        self,
        attempt: PersonaCuriosityAttempt,
    ) -> PersonaCuriosityAttempt:
        self._rows[attempt.id] = attempt
        return attempt

    async def list_recent(
        self,
        character_id: str,
        operator_id: str,
        *,
        limit: int = 8,
    ) -> list[PersonaCuriosityAttempt]:
        rows = [
            row for row in self._rows.values()
            if row.character_id == character_id and row.operator_id == operator_id
        ]
        rows.sort(key=lambda row: row.created_at, reverse=True)
        return rows[: max(0, limit)]

    async def mark_status(
        self,
        attempt_id: str,
        status: str,
        *,
        response_turn_id: str | None = None,
        cooldown_until: datetime | None = None,
    ) -> bool:
        current = self._rows.get(attempt_id)
        if current is None:
            return False
        self._rows[attempt_id] = replace(
            current,
            status=status,
            response_turn_id=response_turn_id or current.response_turn_id,
            cooldown_until=cooldown_until or current.cooldown_until,
        )
        return True
