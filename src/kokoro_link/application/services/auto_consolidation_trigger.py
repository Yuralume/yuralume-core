"""Post-turn auto-trigger for memory decay + consolidation.

Sits between ``ChatService`` and ``MemoryConsolidationService``. After a
turn writes new memories the trigger decides whether now is a good time
to run the expensive pipeline:

- **Threshold gate**: only fire when the character already owns enough
  memories that a merge pass is worth it. Running on small pools just
  burns LLM credits.
- **Cooldown gate**: once we've run for a character, don't run again for
  ``cooldown`` — the pool doesn't grow that fast, and a re-run so soon
  would just thrash.
- **Single-flight lock**: two post-turn tasks for the same character
  can overlap (fire-and-forget). A per-character lock keeps us from
  starting a second consolidation while one is already running.

The caller fires ``maybe_trigger`` fire-and-forget; this class never
raises into the chat path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from kokoro_link.application.services.memory_consolidation_service import (
    MemoryConsolidationService,
)
from kokoro_link.contracts.memory import MemoryRepositoryPort

_LOGGER = logging.getLogger(__name__)


class AutoConsolidationTrigger:
    def __init__(
        self,
        *,
        memory_repository: MemoryRepositoryPort,
        consolidation_service: MemoryConsolidationService,
        threshold: int = 200,
        cooldown: timedelta = timedelta(hours=6),
        clock=None,
    ) -> None:
        self._memory_repository = memory_repository
        self._consolidation_service = consolidation_service
        self._threshold = max(1, threshold)
        self._cooldown = cooldown
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._last_run_at: dict[str, datetime] = {}
        self._running: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def cooldown(self) -> timedelta:
        return self._cooldown

    async def maybe_trigger(self, character_id: str) -> bool:
        """Run consolidation if the pool is full and cooldown has elapsed.

        Returns ``True`` when consolidation actually ran. Safe to call
        from fire-and-forget tasks; all errors are logged and swallowed.
        """
        if not character_id:
            return False
        lock = self._locks.setdefault(character_id, asyncio.Lock())
        if lock.locked() or character_id in self._running:
            return False
        async with lock:
            if not await self._should_run(character_id):
                return False
            self._running.add(character_id)
            self._last_run_at[character_id] = self._clock()
        try:
            report = await self._consolidation_service.consolidate(character_id)
            _LOGGER.info(
                "Auto-consolidation for %s: decayed=%d merged=%d remaining=%d",
                character_id,
                report.decayed,
                report.clusters_merged,
                report.memories_after,
            )
            return True
        except Exception:
            _LOGGER.exception("Auto-consolidation crashed for %s", character_id)
            return False
        finally:
            self._running.discard(character_id)

    async def _should_run(self, character_id: str) -> bool:
        last = self._last_run_at.get(character_id)
        if last is not None and self._clock() - last < self._cooldown:
            return False
        try:
            count = await self._memory_repository.count_for_character(character_id)
        except Exception:
            _LOGGER.exception(
                "Failed to count memories for %s; skipping consolidation", character_id,
            )
            return False
        return count >= self._threshold
