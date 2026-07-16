"""In-process self-reflection store for dev / tests (HUMANIZATION_ROADMAP §3.2)."""

from __future__ import annotations

from kokoro_link.contracts.self_reflection import (
    SelfReflectionRepositoryPort,
)
from kokoro_link.domain.entities.self_reflection import SelfReflection


class InMemorySelfReflectionRepository(SelfReflectionRepositoryPort):
    def __init__(self) -> None:
        # key: (character_id, operator_id, period) → latest row
        self._rows: dict[tuple[str, str, str], SelfReflection] = {}

    async def upsert_latest(
        self, reflection: SelfReflection,
    ) -> SelfReflection:
        key = (
            reflection.character_id,
            reflection.operator_id,
            reflection.period,
        )
        self._rows[key] = reflection
        return reflection

    async def latest_for(
        self, character_id: str, operator_id: str,
    ) -> list[SelfReflection]:
        matches = [
            row for (cid, op, _period), row in self._rows.items()
            if cid == character_id and op == operator_id
        ]
        matches.sort(key=lambda r: r.created_at, reverse=True)
        return matches

    async def delete_for_character(self, character_id: str) -> int:
        keys = [k for k in self._rows if k[0] == character_id]
        for k in keys:
            del self._rows[k]
        return len(keys)
