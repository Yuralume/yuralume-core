from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import Character


class InMemoryCharacterRepository(CharacterRepositoryPort):
    def __init__(self) -> None:
        self._characters: dict[str, Character] = {}

    async def list(self) -> list[Character]:
        return list(self._characters.values())

    async def list_for_user(self, user_id: str) -> list[Character]:
        return [c for c in self._characters.values() if c.user_id == user_id]

    async def list_active(self) -> list[Character]:
        return [
            c for c in self._characters.values()
            if not c.frozen and not c.subscription_locked
        ]

    async def get(self, character_id: str) -> Character | None:
        return self._characters.get(character_id)

    async def save(self, character: Character) -> None:
        existing = self._characters.get(character.id)
        if existing is None:
            self._characters[character.id] = character
            return
        self._characters[character.id] = replace(
            character,
            frozen=existing.frozen,
            frozen_at=existing.frozen_at,
            frozen_reason=existing.frozen_reason,
            subscription_locked=existing.subscription_locked,
        )

    async def set_frozen(
        self,
        character_id: str,
        *,
        frozen: bool,
        now: datetime,
        reason: str | None = None,
    ) -> bool:
        existing = self._characters.get(character_id)
        if existing is None:
            return False
        self._characters[character_id] = replace(
            existing,
            frozen=frozen,
            frozen_at=now if frozen else None,
            frozen_reason=reason if frozen else None,
        )
        return True

    async def set_subscription_locked(
        self, character_id: str, *, locked: bool,
    ) -> bool:
        existing = self._characters.get(character_id)
        if existing is None:
            return False
        self._characters[character_id] = replace(
            existing, subscription_locked=bool(locked),
        )
        return True

    async def delete(self, character_id: str) -> bool:
        return self._characters.pop(character_id, None) is not None
