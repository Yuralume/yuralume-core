"""In-memory persona curiosity ledger for tests / local dev."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from kokoro_link.contracts.persona_curiosity import (
    PersonaCuriosityRepositoryPort,
)
from kokoro_link.domain.entities.persona_curiosity import (
    PersonaCuriosityAttempt,
)


def _ensure_tz(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


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

    async def delete_created_since(
        self, character_id: str, conversation_id: str, since: datetime,
    ) -> int:
        moment = _ensure_tz(since)
        target = [
            row_id for row_id, row in self._rows.items()
            if row.character_id == character_id
            and row.conversation_id == conversation_id
            and _ensure_tz(row.created_at) >= moment
        ]
        for row_id in target:
            del self._rows[row_id]
        return len(target)
