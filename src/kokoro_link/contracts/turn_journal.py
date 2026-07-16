"""TurnJournal repository port.

Storage-agnostic CRUD for rollback records. In-memory adapter is used
by unit tests; SQLAlchemy adapter persists in the ``turn_journals`` table.
"""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.turn_journal import TurnJournal


class TurnJournalRepositoryPort(Protocol):
    async def add(self, journal: TurnJournal) -> None:
        """Persist a new journal row."""

    async def save(self, journal: TurnJournal) -> None:
        """Upsert — used when the service finalises ``added_*`` IDs."""

    async def get_latest(self, conversation_id: str) -> TurnJournal | None:
        """Return the most recent journal for ``conversation_id``, or ``None``.

        "Most recent" = highest ``turn_index`` (ties broken by ``created_at``).
        """

    async def list_for_conversation(
        self, conversation_id: str, *, limit: int = 5,
    ) -> list[TurnJournal]:
        """Return journals newest-first, capped at ``limit``."""

    async def delete(self, journal_id: str) -> bool:
        """Remove a single journal row. ``True`` when a row was removed."""

    async def prune_for_conversation(
        self, conversation_id: str, *, keep: int = 5,
    ) -> int:
        """Keep the ``keep`` most recent journals for the conversation; delete
        the rest. Returns the number removed. Idempotent when the pool is
        already under ``keep``.
        """

    async def delete_for_conversation(self, conversation_id: str) -> int:
        """Cascade-delete every journal for the conversation."""

    async def delete_for_character(self, character_id: str) -> int:
        """Cascade-delete every journal belonging to a character.

        Used by ``CharacterService.delete_character`` so journals don't
        outlive their owning character.
        """
