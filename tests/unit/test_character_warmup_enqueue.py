"""Durable warmup enqueue on character create (HOSTED_CORE_SCALING §13 P3-C).

When the queue + coordinator lease are wired AND warmup is enabled (hosted
execution backend), creating a character enqueues a ``character_warmup`` job so a
new character is ready before its first bucket. At-least-once + fenced against the
current coordinator epoch; an enqueue failure is logged, never fatal (the next
character_tick self-heals). Self-host default (disabled) enqueues nothing.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.background_shadow_coordinator import (
    CHARACTER_WARMUP_KIND,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.contracts.background_jobs import COORDINATOR_LEASE_NAME
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
    InMemoryBackgroundJobQueue,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


class _Clock:
    def now(self) -> datetime:
        return NOW


async def _leader_lease() -> InMemoryBackgroundCoordinatorLease:
    lease = InMemoryBackgroundCoordinatorLease()
    await lease.acquire(COORDINATOR_LEASE_NAME, "coord", ttl_seconds=30, now=NOW)
    return lease


async def test_warmup_enqueued_on_create_when_enabled() -> None:
    lease = await _leader_lease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    service = CharacterService(
        InMemoryCharacterRepository(),
        background_job_queue=queue,
        background_coordinator_lease=lease,
        warmup_enqueue_enabled=True,
        clock=_Clock(),
    )
    created = await service.create_character(CreateCharacterRequest(name="Airi"))
    claimed = await queue.claim("w1", now=NOW, limit=10, lease_seconds=60)
    assert len(claimed) == 1
    job = claimed[0]
    assert job.kind == CHARACTER_WARMUP_KIND
    assert job.character_id == created.id
    assert job.idempotency_key == f"{CHARACTER_WARMUP_KIND}:{created.id}"


async def test_warmup_not_enqueued_when_disabled() -> None:
    lease = await _leader_lease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    service = CharacterService(
        InMemoryCharacterRepository(),
        background_job_queue=queue,
        background_coordinator_lease=lease,
        warmup_enqueue_enabled=False,
    )
    await service.create_character(CreateCharacterRequest(name="Airi"))
    assert await queue.claim("w1", now=NOW, limit=10, lease_seconds=60) == []


async def test_warmup_not_enqueued_without_leader() -> None:
    # No leader holds the coordinator lease → the enqueue would be fenced out,
    # so it is skipped (self-heals on the next tick). Character still created.
    lease = InMemoryBackgroundCoordinatorLease()  # never acquired
    queue = InMemoryBackgroundJobQueue(lease=lease)
    service = CharacterService(
        InMemoryCharacterRepository(),
        background_job_queue=queue,
        background_coordinator_lease=lease,
        warmup_enqueue_enabled=True,
        clock=_Clock(),
    )
    created = await service.create_character(CreateCharacterRequest(name="Airi"))
    assert created.id is not None
    assert await queue.claim("w1", now=NOW, limit=10, lease_seconds=60) == []


async def test_warmup_enqueue_failure_is_not_fatal() -> None:
    lease = await _leader_lease()

    class _ExplodingQueue(InMemoryBackgroundJobQueue):
        async def enqueue(self, spec, *, now):
            raise RuntimeError("queue down")

    queue = _ExplodingQueue(lease=lease)
    repo = InMemoryCharacterRepository()
    service = CharacterService(
        repo,
        background_job_queue=queue,
        background_coordinator_lease=lease,
        warmup_enqueue_enabled=True,
        clock=_Clock(),
    )
    created = await service.create_character(CreateCharacterRequest(name="Airi"))
    # Character was still created despite the enqueue blowing up.
    assert await repo.get(created.id) is not None
