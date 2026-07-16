"""In-process memoir pin store for dev / tests.

See ``docs/MEMOIR_PLAN.md`` for the design rationale and
``src/kokoro_link/contracts/memoir.py`` for the port spec.
"""

from __future__ import annotations

from kokoro_link.contracts.memoir import MemoirPinRepositoryPort
from kokoro_link.domain.entities.memoir_pin import MemoirPin


class InMemoryMemoirPinRepository(MemoirPinRepositoryPort):
    def __init__(self) -> None:
        # key: (character_id, operator_id, entry_kind, entry_id) → pin
        self._rows: dict[tuple[str, str, str, str], MemoirPin] = {}

    @staticmethod
    def _key(pin: MemoirPin) -> tuple[str, str, str, str]:
        return (pin.character_id, pin.operator_id, pin.entry_kind, pin.entry_id)

    async def list_for(
        self, character_id: str, operator_id: str,
    ) -> list[MemoirPin]:
        matches = [
            pin for pin in self._rows.values()
            if pin.character_id == character_id
            and pin.operator_id == operator_id
        ]
        matches.sort(key=lambda p: p.pinned_at, reverse=True)
        return matches

    async def add(self, pin: MemoirPin) -> MemoirPin:
        key = self._key(pin)
        existing = self._rows.get(key)
        if existing is not None:
            return existing
        self._rows[key] = pin
        return pin

    async def remove(
        self,
        character_id: str,
        operator_id: str,
        entry_kind: str,
        entry_id: str,
    ) -> bool:
        key = (character_id, operator_id, entry_kind, entry_id)
        return self._rows.pop(key, None) is not None

    async def count_for(
        self, character_id: str, operator_id: str,
    ) -> int:
        return sum(
            1 for pin in self._rows.values()
            if pin.character_id == character_id
            and pin.operator_id == operator_id
        )

    async def delete_for_character(self, character_id: str) -> int:
        keys = [k for k, pin in self._rows.items() if pin.character_id == character_id]
        for k in keys:
            del self._rows[k]
        return len(keys)
