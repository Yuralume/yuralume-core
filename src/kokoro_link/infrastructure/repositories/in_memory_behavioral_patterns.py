"""In-process behavioural-pattern store for dev / tests (HUMANIZATION_ROADMAP §3.3)."""

from __future__ import annotations

from dataclasses import replace

from kokoro_link.contracts.behavioral_pattern import (
    BehavioralPatternRepositoryPort,
)
from kokoro_link.domain.entities.behavioral_pattern import BehavioralPattern


def _key(pattern: BehavioralPattern) -> tuple[str, str, str]:
    return (pattern.character_id, pattern.kind, pattern.description)


class InMemoryBehavioralPatternRepository(BehavioralPatternRepositoryPort):
    def __init__(self) -> None:
        self._rows: dict[tuple[str, str, str], BehavioralPattern] = {}

    async def upsert(self, pattern: BehavioralPattern) -> BehavioralPattern:
        key = _key(pattern)
        existing = self._rows.get(key)
        if existing is None:
            self._rows[key] = pattern
            return pattern
        merged = replace(
            existing,
            observed_count=existing.observed_count + pattern.observed_count,
            last_observed_at=max(existing.last_observed_at, pattern.last_observed_at),
            salience=max(existing.salience, pattern.salience),
        )
        self._rows[key] = merged
        return merged

    async def list_for_character(
        self,
        character_id: str,
        *,
        kinds: tuple[str, ...] | None = None,
        limit: int = 12,
    ) -> list[BehavioralPattern]:
        kind_filter = set(kinds) if kinds else None
        matches = [
            row for row in self._rows.values()
            if row.character_id == character_id
            and (kind_filter is None or row.kind in kind_filter)
        ]
        matches.sort(
            key=lambda p: (p.observed_count * p.salience, p.last_observed_at),
            reverse=True,
        )
        return matches[: max(1, limit)]

    async def delete_for_character(self, character_id: str) -> int:
        keys = [k for k in self._rows if k[0] == character_id]
        for k in keys:
            del self._rows[k]
        return len(keys)
