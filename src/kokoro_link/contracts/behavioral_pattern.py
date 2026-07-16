"""Repository port for ``BehavioralPattern`` (HUMANIZATION_ROADMAP §3.3)."""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.behavioral_pattern import BehavioralPattern


class BehavioralPatternRepositoryPort(Protocol):
    async def upsert(self, pattern: BehavioralPattern) -> BehavioralPattern:
        """Insert ``pattern`` or update an existing row keyed by
        ``(character_id, kind, description)``.

        Upsert (instead of plain add) so the dream pass can rerun
        weekly without bloating the table — repeated detections
        bump ``observed_count`` and ``last_observed_at`` in place.
        Returns the persisted row."""

    async def list_for_character(
        self,
        character_id: str,
        *,
        kinds: tuple[str, ...] | None = None,
        limit: int = 12,
    ) -> list[BehavioralPattern]:
        """Return the strongest patterns for a character, ordered by
        observed_count × salience desc."""

    async def delete_for_character(self, character_id: str) -> int:
        """Wipe every pattern for a character. Used by the data-purge CLI."""
