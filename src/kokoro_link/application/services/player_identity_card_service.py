"""CRUD for 玩家身分卡 — the player's reusable creation templates.

Everything the API layer would otherwise have to know lives here: the
per-operator cap, what a same-name save means, and the rule that a card
is only ever reachable by the operator who owns it. The routes translate
the two typed errors below into status codes and nothing else.

There is deliberately no "apply" operation. Applying a card is the client
filling the creation wizard from it — the values then travel the existing
character-creation and persona-note paths, and the copy stays a copy.
"""

from __future__ import annotations

from datetime import datetime, timezone

from kokoro_link.contracts.player_identity_card import (
    PlayerIdentityCardNameConflictError,
    PlayerIdentityCardRepositoryPort,
)
from kokoro_link.domain.entities.player_identity_card import (
    PLAYER_IDENTITY_CARDS_PER_OPERATOR,
    PlayerIdentityCard,
)


__all__ = [
    "PlayerIdentityCardLimitReachedError",
    # Re-exported from the port: the repository raises it too, when two
    # concurrent saves slip past the pre-check below and collide on the
    # ``(operator_id, name)`` constraint. Callers import it from here.
    "PlayerIdentityCardNameConflictError",
    "PlayerIdentityCardService",
]


class PlayerIdentityCardLimitReachedError(Exception):
    """The operator is at the per-account cap."""

    def __init__(self, *, current: int, limit: int) -> None:
        super().__init__(f"identity card limit reached ({current}/{limit})")
        self.current = current
        self.limit = limit


class PlayerIdentityCardService:
    def __init__(
        self,
        repository: PlayerIdentityCardRepositoryPort,
        *,
        limit: int = PLAYER_IDENTITY_CARDS_PER_OPERATOR,
    ) -> None:
        self._repository = repository
        self._limit = limit

    @property
    def limit(self) -> int:
        return self._limit

    async def list_cards(self, operator_id: str) -> list[PlayerIdentityCard]:
        return await self._repository.list_for_operator(operator_id)

    async def get_card(
        self, *, card_id: str, operator_id: str,
    ) -> PlayerIdentityCard | None:
        return await self._repository.get(
            card_id=card_id, operator_id=operator_id,
        )

    async def save_card(
        self,
        *,
        operator_id: str,
        name: str,
        overwrite: bool = False,
        now: datetime | None = None,
        **content: object,
    ) -> PlayerIdentityCard:
        """Create a card, or overwrite the same-named one on request.

        Raises :class:`PlayerIdentityCardNameConflictError` when the name
        is taken and ``overwrite`` is false, and
        :class:`PlayerIdentityCardLimitReachedError` when a *new* card
        would exceed the cap — an overwrite replaces a row rather than
        adding one, so it is allowed at the cap.

        The name check below and the write that follows it are not one
        transaction, so the same error can also come back out of
        ``upsert``: two concurrent saves of one new name both read "not
        taken" here and only collide at the store. That is the same
        conflict, reported from the only place that can still see it —
        it is deliberately *not* re-checked or retried here, because the
        answer ("that name is taken, overwrite?") is identical and the
        player has to make the same choice either way.
        """
        stamped = now or datetime.now(timezone.utc)
        candidate = PlayerIdentityCard.create(
            operator_id=operator_id, name=name, now=stamped, **content,
        )
        existing = await self._repository.find_by_name(
            operator_id=operator_id, name=candidate.name,
        )
        if existing is not None:
            if not overwrite:
                raise PlayerIdentityCardNameConflictError(existing)
            merged = existing.overwritten_by(candidate, now=stamped)
            await self._repository.upsert(merged)
            return merged

        current = await self._repository.count_for_operator(operator_id)
        if current >= self._limit:
            raise PlayerIdentityCardLimitReachedError(
                current=current, limit=self._limit,
            )
        await self._repository.upsert(candidate)
        return candidate

    async def rename_card(
        self,
        *,
        card_id: str,
        operator_id: str,
        name: str,
        now: datetime | None = None,
    ) -> PlayerIdentityCard | None:
        """Change a card's label. ``None`` when the operator has no such card.

        No overwrite semantics here: merging two cards because their
        labels collided would destroy content the player never asked to
        replace, so a clash is simply refused.
        """
        existing = await self._repository.get(
            card_id=card_id, operator_id=operator_id,
        )
        if existing is None:
            return None
        renamed = existing.renamed(name, now=now or datetime.now(timezone.utc))
        if renamed.name != existing.name:
            clash = await self._repository.find_by_name(
                operator_id=operator_id, name=renamed.name,
            )
            if clash is not None and clash.id != existing.id:
                raise PlayerIdentityCardNameConflictError(clash)
        await self._repository.upsert(renamed)
        return renamed

    async def delete_card(self, *, card_id: str, operator_id: str) -> bool:
        return await self._repository.delete(
            card_id=card_id, operator_id=operator_id,
        )
