from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Callable

from kokoro_link.contracts.repositories import CharacterRepositoryPort
from kokoro_link.domain.entities.character import Character


class InMemoryCharacterRepository(CharacterRepositoryPort):
    def __init__(
        self, *, clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._characters: dict[str, Character] = {}
        # Consolidation claim ledger — the in-process twin of the
        # ``characters.last_consolidated_at`` column. Kept beside the entities
        # rather than on them because it is a dedicated control field the
        # aggregate ``save()`` must never write (same rule as the SA adapter).
        self._consolidation_claims: dict[str, datetime] = {}
        # Injectable so cooldown tests can travel in time; the SA adapter's
        # equivalent authority is the DB clock.
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    async def claim_consolidation_slot(
        self, character_id: str, *, cooldown: timedelta,
    ) -> bool:
        """Single-process twin of the conditional UPDATE.

        Deliberately independent of whether the character was ever ``save``d:
        this adapter backs no-DB runs and lease-less test rigs, where the
        historical gate was a bare per-process dict with no row behind it, so
        requiring one would change behaviour rather than preserve it."""
        now = self._clock()
        last = self._consolidation_claims.get(character_id)
        if last is not None and now - last < cooldown:
            return False
        self._consolidation_claims[character_id] = now
        return True

    async def touch_last_active(self, character_id: str, now: datetime) -> bool:
        """Single-process twin of the targeted, monotonic anchor UPDATE."""
        character = self._characters.get(character_id)
        if character is None:
            return False
        current = character.state.last_active_at
        if current is not None and current >= now:
            return False
        self._characters[character_id] = character.with_state(
            character.state.with_active_now(now),
        )
        return True

    async def list(self) -> list[Character]:
        return list(self._characters.values())

    async def list_for_user(self, user_id: str) -> list[Character]:
        return [c for c in self._characters.values() if c.user_id == user_id]

    async def list_active(self) -> list[Character]:
        return [
            c for c in self._characters.values()
            if not c.frozen and not c.subscription_locked
        ]

    async def list_by_origin_official_card_id(
        self, card_id: str,
    ) -> list[Character]:
        return [
            c for c in self._characters.values()
            if c.origin_official_card_id == card_id
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
