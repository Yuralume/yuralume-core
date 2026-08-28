"""Repository port for :class:`PlayerIdentityCard`."""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.player_identity_card import PlayerIdentityCard


class PlayerIdentityCardNameConflictError(Exception):
    """This operator already has a card under that name.

    Lives on the port rather than in the service because *both* sides
    raise it: the service's pre-check (a name the caller can see is
    taken) and the repository's ``(operator_id, name)`` unique
    constraint (two saves that both passed that pre-check and only
    collide at the write). One class means the route's single 409
    mapping covers both — a driver-specific ``IntegrityError`` escaping
    to the API would be a 500 for what is, to the player, the same
    ordinary "that name is taken".

    Carries the existing card so the caller can offer "overwrite it?"
    without a second round trip.
    """

    def __init__(self, existing: PlayerIdentityCard) -> None:
        super().__init__(f"identity card named {existing.name!r} already exists")
        self.existing = existing


class PlayerIdentityCardRepositoryPort(Protocol):
    async def list_for_operator(
        self, operator_id: str,
    ) -> list[PlayerIdentityCard]:
        """Every card this operator owns, newest update first.

        Ordering is part of the contract: the picker shows the list as
        given, and "the one I just saved" being at the top is what makes
        a re-save feel like an edit rather than a duplicate."""
        ...

    async def get(
        self, *, card_id: str, operator_id: str,
    ) -> PlayerIdentityCard | None:
        """One card, scoped to its owner.

        ``operator_id`` is a filter, not an assertion — another
        operator's id must read as "no such card" so the API can answer
        404 without leaking that the id exists."""
        ...

    async def find_by_name(
        self, *, operator_id: str, name: str,
    ) -> PlayerIdentityCard | None:
        """The operator's card with this exact (already trimmed) name.

        Backs the same-name conflict check. Exact match on purpose: the
        DB's unique constraint is exact, and an app-level check that
        collapsed case would refuse saves the database would have
        accepted."""
        ...

    async def count_for_operator(self, operator_id: str) -> int:
        """How many cards this operator holds, for the per-operator cap."""
        ...

    async def upsert(self, card: PlayerIdentityCard) -> None:
        """Insert the card, or replace the row with the same ``id``.

        Raises :class:`PlayerIdentityCardNameConflictError` when a
        *different* card of the same operator already holds this name.
        The service checks for that first, but the check and this write
        are not one transaction: two concurrent saves of the same new
        name both find nothing and both insert, and only the store sees
        the collision. Adapters must report it as this typed error so
        the loser of that race gets the same 409 the pre-check would
        have produced."""
        ...

    async def delete(self, *, card_id: str, operator_id: str) -> bool:
        """Remove the operator's card. Returns whether a row was deleted."""
        ...
