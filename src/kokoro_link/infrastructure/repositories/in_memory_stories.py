"""In-memory StorySeed + StoryEvent repositories (tests / fake-provider)."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from threading import RLock

from kokoro_link.contracts.story import (
    StoryEventRepositoryPort,
    StorySeedRepositoryPort,
)
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.entities.story_seed import StorySeed


class InMemoryStorySeedRepository(StorySeedRepositoryPort):
    def __init__(self) -> None:
        self._seeds: dict[str, StorySeed] = {}
        self._lock = RLock()

    async def upsert_by_external_id(self, seed: StorySeed) -> StorySeed:
        if not seed.external_id:
            raise ValueError("upsert_by_external_id requires external_id")
        with self._lock:
            existing = next(
                (s for s in self._seeds.values() if s.external_id == seed.external_id),
                None,
            )
            if existing is None:
                self._seeds[seed.id] = seed
                return seed
            updated = replace(
                existing,
                seed_text=seed.seed_text,
                tags=seed.tags,
                world_frames=seed.world_frames,
                weight=seed.weight,
                cooldown_days=seed.cooldown_days,
                enabled=seed.enabled,
                language=seed.language,
                character_id=seed.character_id,
                pack_id=seed.pack_id,
                updated_at=datetime.now(timezone.utc),
            )
            self._seeds[existing.id] = updated
        return updated

    async def add(self, seed: StorySeed) -> StorySeed:
        with self._lock:
            self._seeds[seed.id] = seed
        return seed

    async def get(self, seed_id: str) -> StorySeed | None:
        with self._lock:
            return self._seeds.get(seed_id)

    async def list_for_character(
        self,
        character_id: str,
        *,
        include_global: bool = True,
        enabled_only: bool = True,
    ) -> list[StorySeed]:
        with self._lock:
            candidates = list(self._seeds.values())
        filtered: list[StorySeed] = []
        for seed in candidates:
            if seed.character_id is None:
                if not include_global:
                    continue
            elif seed.character_id != character_id:
                continue
            if enabled_only and not seed.enabled:
                continue
            filtered.append(seed)
        filtered.sort(key=lambda s: s.created_at)
        return filtered

    async def list_by_pack(self, pack_id: str) -> list[StorySeed]:
        with self._lock:
            return [s for s in self._seeds.values() if s.pack_id == pack_id]

    async def update(self, seed: StorySeed) -> StorySeed:
        with self._lock:
            if seed.id not in self._seeds:
                raise ValueError(f"StorySeed {seed.id} not found")
            updated = replace(seed, updated_at=datetime.now(timezone.utc))
            self._seeds[seed.id] = updated
        return updated

    async def delete(self, seed_id: str) -> bool:
        with self._lock:
            return self._seeds.pop(seed_id, None) is not None


class InMemoryStoryEventRepository(StoryEventRepositoryPort):
    def __init__(self) -> None:
        self._events: dict[str, StoryEvent] = {}
        self._lock = RLock()

    async def add(self, event: StoryEvent) -> StoryEvent:
        with self._lock:
            # Enforce the same (character_id, date, seed_id) and
            # (character_id, date, arc_beat_id) uniqueness as the SA
            # version so tests catch accidental double-roll — either
            # double gacha or double arc-beat realization on the same
            # civil day. NULL keys don't collide (SQL semantics).
            for existing in self._events.values():
                if (
                    existing.character_id == event.character_id
                    and existing.date == event.date
                ):
                    if (
                        event.seed_id is not None
                        and existing.seed_id == event.seed_id
                    ):
                        raise ValueError(
                            "duplicate (character_id, date, seed_id) for StoryEvent",
                        )
                    if (
                        event.arc_beat_id is not None
                        and existing.arc_beat_id == event.arc_beat_id
                    ):
                        raise ValueError(
                            "duplicate (character_id, date, arc_beat_id) for StoryEvent",
                        )
            self._events[event.id] = event
        return event

    async def get_for_day(
        self, character_id: str, date: str,
    ) -> list[StoryEvent]:
        with self._lock:
            matches = [
                e for e in self._events.values()
                if e.character_id == character_id and e.date == date
            ]
        matches.sort(key=lambda e: e.created_at)
        return matches

    async def list_recent(
        self, character_id: str, *, limit: int = 10,
    ) -> list[StoryEvent]:
        with self._lock:
            matches = [
                e for e in self._events.values()
                if e.character_id == character_id
            ]
        matches.sort(key=lambda e: (e.date, e.created_at), reverse=True)
        return matches[:limit]

    async def last_roll_dates(
        self, character_id: str,
    ) -> dict[str, str]:
        # Gacha cooldown cares only about seed-driven events; arc beats
        # have their own scheduling mechanism and NULL seed_id here.
        result: dict[str, str] = {}
        with self._lock:
            for event in self._events.values():
                if event.character_id != character_id:
                    continue
                if event.seed_id is None:
                    continue
                current = result.get(event.seed_id)
                if current is None or event.date > current:
                    result[event.seed_id] = event.date
        return result

    async def mark_memorialized(self, event_id: str) -> None:
        with self._lock:
            existing = self._events.get(event_id)
            if existing is None:
                return
            self._events[event_id] = existing.marked_memorialized()

    async def delete_for_character(self, character_id: str) -> int:
        with self._lock:
            to_remove = [
                eid for eid, e in self._events.items()
                if e.character_id == character_id
            ]
            for eid in to_remove:
                del self._events[eid]
        return len(to_remove)
