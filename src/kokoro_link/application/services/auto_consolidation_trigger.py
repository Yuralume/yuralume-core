"""Post-turn auto-trigger for memory decay + consolidation.

Sits between ``ChatService`` and ``MemoryConsolidationService``. After a
turn writes new memories the trigger decides whether now is a good time
to run the expensive pipeline:

- **Threshold gate**: only fire when the character already owns enough
  memories that a merge pass is worth it. Running on small pools just
  burns LLM credits.
- **Cooldown + single-flight claim**: one atomic
  ``claim_consolidation_slot`` on the character repository. It stamps
  ``last_consolidated_at`` only when the previous stamp is older than the
  cooldown, so the same statement answers both "has the cooldown elapsed?"
  and "is anyone else already running?" — across processes.

Why the claim is not in-process state: ``maybe_trigger`` is fired
fire-and-forget from the chat turn, and the hosted topology serves chat from
several api replicas. The previous ``_last_run_at`` dict / ``_running`` set /
``asyncio.Lock`` trio was per-process, so two replicas could run
``consolidate()`` — an LLM-heavy pass that rewrites memory rows — for the same
character at the same time and overwrite each other, while the effective
cooldown shrank by a factor of the replica count. The lock and the ``_running``
set survive as a *cheap local gate* that spares the DB a round-trip for the
same-process overlap; correctness now rests entirely on the claim.

The caller fires ``maybe_trigger`` fire-and-forget; this class never
raises into the chat path.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from kokoro_link.application.services.memory_consolidation_service import (
    MemoryConsolidationService,
)
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.repositories import CharacterRepositoryPort

_LOGGER = logging.getLogger(__name__)


class AutoConsolidationTrigger:
    def __init__(
        self,
        *,
        memory_repository: MemoryRepositoryPort,
        consolidation_service: MemoryConsolidationService,
        character_repository: CharacterRepositoryPort,
        threshold: int = 200,
        cooldown: timedelta = timedelta(hours=6),
    ) -> None:
        self._memory_repository = memory_repository
        self._consolidation_service = consolidation_service
        self._characters = character_repository
        self._threshold = max(1, threshold)
        self._cooldown = cooldown
        self._running: set[str] = set()
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def threshold(self) -> int:
        return self._threshold

    @property
    def cooldown(self) -> timedelta:
        return self._cooldown

    async def maybe_trigger(self, character_id: str) -> bool:
        """Run consolidation if the pool is full and the claim is ours.

        Returns ``True`` when consolidation actually ran. Safe to call
        from fire-and-forget tasks; all errors are logged and swallowed.
        """
        if not character_id:
            return False
        lock = self._locks.setdefault(character_id, asyncio.Lock())
        if lock.locked() or character_id in self._running:
            # Cheap local gate: a same-process overlap is already decided, so
            # skip the count query and the claim round-trip entirely.
            return False
        async with lock:
            if not await self._pool_is_large_enough(character_id):
                return False
            if not await self._claim(character_id):
                return False
            self._running.add(character_id)
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
            # The claim is deliberately NOT released: it was consumed on
            # attempt, exactly as the pre-distributed code stamped its run
            # timestamp before running, so a consolidation that keeps crashing
            # cannot re-enter on every subsequent turn.
            _LOGGER.exception("Auto-consolidation crashed for %s", character_id)
            return False
        finally:
            self._running.discard(character_id)

    async def _claim(self, character_id: str) -> bool:
        try:
            return await self._characters.claim_consolidation_slot(
                character_id, cooldown=self._cooldown,
            )
        except Exception:
            # Fail closed: an unclaimable slot means we cannot prove we are the
            # only runner, and a duplicated consolidation corrupts memories.
            _LOGGER.exception(
                "Failed to claim consolidation slot for %s; skipping",
                character_id,
            )
            return False

    async def _pool_is_large_enough(self, character_id: str) -> bool:
        try:
            count = await self._memory_repository.count_for_character(character_id)
        except Exception:
            _LOGGER.exception(
                "Failed to count memories for %s; skipping consolidation", character_id,
            )
            return False
        return count >= self._threshold
