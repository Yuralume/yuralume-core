"""Centralised state-change recording.

Thin wrapper around ``StateHistoryRepositoryPort`` that builds
``StateSnapshot`` instances from a before/after state pair. Services
call ``record()`` whenever the character state is mutated; the tracker
decides whether the change is worth recording (skip no-ops).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from kokoro_link.contracts.state_history import StateHistoryRepositoryPort
from kokoro_link.domain.entities.state_snapshot import StateSnapshot
from kokoro_link.domain.value_objects.character_state import CharacterState

_LOGGER = logging.getLogger(__name__)


class StateChangeTracker:
    def __init__(self, repository: StateHistoryRepositoryPort) -> None:
        self._repository = repository

    async def record(
        self,
        *,
        character_id: str,
        source: str,
        before: CharacterState,
        after: CharacterState,
        trigger: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Record a state change if any value actually changed."""
        if _is_same(before, after):
            return
        snapshot = StateSnapshot.from_state(
            character_id=character_id,
            source=source,
            state=after,
            trigger=trigger,
            created_at=now or datetime.now(timezone.utc),
        )
        try:
            await self._repository.add(snapshot)
        except Exception:
            _LOGGER.exception("Failed to record state change")


def _is_same(a: CharacterState, b: CharacterState) -> bool:
    return (
        a.emotion == b.emotion
        and a.affection == b.affection
        and a.fatigue == b.fatigue
        and a.trust == b.trust
        and a.energy == b.energy
    )
