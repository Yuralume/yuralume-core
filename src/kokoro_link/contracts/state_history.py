"""State history repository port."""

from typing import Protocol

from kokoro_link.domain.entities.state_snapshot import StateSnapshot


class StateHistoryRepositoryPort(Protocol):
    async def add(self, snapshot: StateSnapshot) -> None:
        """Persist a state-change snapshot."""

    async def query(
        self,
        character_id: str,
        *,
        limit: int = 50,
    ) -> list[StateSnapshot]:
        """Return recent snapshots ordered newest-first."""

    async def delete_many(self, snapshot_ids: list[str]) -> int:
        """Delete a batch of snapshots by id. Returns the number removed.

        Used by the turn-undo path to reverse per-turn state changes
        without dropping unrelated history. Missing ids are ignored.
        """

    async def delete_created_since(
        self, character_id: str, since,
    ) -> int:
        """Delete snapshots for ``character_id`` created at-or-after ``since``.

        Used by turn-undo to reverse all state changes that landed during
        the turn window (heuristic, LLM refinement, rest recovery — they
        all funnel through ``StateChangeTracker.record`` so catching them
        by character + time is simpler than threading ids back out).
        """

    async def delete_for_character(self, character_id: str) -> int:
        """Remove all snapshots for a character. Returns count."""
