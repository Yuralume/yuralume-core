"""In-memory :class:`PlayerIdentityCard` store for tests / dev."""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.contracts.player_identity_card import (
    PlayerIdentityCardNameConflictError,
    PlayerIdentityCardRepositoryPort,
)
from kokoro_link.domain.entities.player_identity_card import PlayerIdentityCard


_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


class InMemoryPlayerIdentityCardRepository(PlayerIdentityCardRepositoryPort):
    def __init__(self) -> None:
        self._rows: dict[str, PlayerIdentityCard] = {}

    async def list_for_operator(
        self, operator_id: str,
    ) -> list[PlayerIdentityCard]:
        owned = [
            card for card in self._rows.values()
            if card.operator_id == operator_id
        ]
        # Same ordering as the SQL adapter: newest update first, id as
        # the tie-break so a test that saves twice in one instant still
        # sees a stable list.
        owned.sort(key=lambda card: (card.updated_at or _EPOCH, card.id), reverse=True)
        return owned

    async def get(
        self, *, card_id: str, operator_id: str,
    ) -> PlayerIdentityCard | None:
        card = self._rows.get(card_id)
        if card is None or card.operator_id != operator_id:
            return None
        return card

    def _by_name(self, operator_id: str, name: str) -> PlayerIdentityCard | None:
        """The stored row for this exact name, bypassing the port.

        ``upsert``'s constraint check goes through here rather than
        through :meth:`find_by_name` so that a subclass which blinds the
        *query* (to reproduce a pre-check that ran before the other
        writer committed) still meets the *constraint* — exactly as a
        stale SELECT does not disable a unique index.
        """
        wanted = (name or "").strip()
        for card in self._rows.values():
            if card.operator_id == operator_id and card.name == wanted:
                return card
        return None

    async def find_by_name(
        self, *, operator_id: str, name: str,
    ) -> PlayerIdentityCard | None:
        return self._by_name(operator_id, name)

    async def count_for_operator(self, operator_id: str) -> int:
        return sum(
            1 for card in self._rows.values() if card.operator_id == operator_id
        )

    async def upsert(self, card: PlayerIdentityCard) -> None:
        existing = self._rows.get(card.id)
        if existing is not None and existing.operator_id != card.operator_id:
            raise ValueError(
                f"identity card {card.id} belongs to another operator",
            )
        clash = self._by_name(card.operator_id, card.name)
        if clash is not None and clash.id != card.id:
            # Stands in for the (operator_id, name) unique constraint,
            # down to the error the SQL adapter raises: a test that only
            # ever runs against this double must see the same 409 the
            # database path produces, not a bare ValueError that would
            # have been a 500 in production.
            raise PlayerIdentityCardNameConflictError(clash)
        self._rows[card.id] = card

    async def delete(self, *, card_id: str, operator_id: str) -> bool:
        card = self._rows.get(card_id)
        if card is None or card.operator_id != operator_id:
            return False
        del self._rows[card_id]
        return True
