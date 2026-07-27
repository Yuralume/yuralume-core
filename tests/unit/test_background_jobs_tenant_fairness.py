"""Per-tenant fairness in claim() (HOSTED_CORE_SCALING §13 Phase 3-C / §5).

A single claim batch keeps at most one job per tenant, and a tenant that already
has a LIVE claimed job elsewhere is excluded — so one busy tenant can't
monopolise a worker's batch. ``tenant_id=None`` jobs (social_tick, local soak)
are exempt. This is the in-memory twin of the SA correlated-NOT-EXISTS + batch
dedup; the PG integration proves the same under real concurrency.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.contracts.background_jobs import BackgroundJobSpec
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundJobQueue,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def _spec(key: str, *, tenant_id: str | None, priority: int = 3) -> BackgroundJobSpec:
    return BackgroundJobSpec(
        kind="character_tick",
        idempotency_key=key,
        due_at=BASE,
        fencing_epoch=0,
        priority=priority,
        tenant_id=tenant_id,
        character_id=key,
    )


async def test_batch_keeps_at_most_one_per_tenant() -> None:
    queue = InMemoryBackgroundJobQueue()
    for i in range(3):
        await queue.enqueue(_spec(f"a{i}", tenant_id="tenant-a"), now=BASE)
    for i in range(3):
        await queue.enqueue(_spec(f"b{i}", tenant_id="tenant-b"), now=BASE)

    claimed = await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    tenants = [c.tenant_id for c in claimed]
    assert sorted(tenants) == ["tenant-a", "tenant-b"]  # one each, extras left


async def test_none_tenant_jobs_are_exempt() -> None:
    queue = InMemoryBackgroundJobQueue()
    ids = []
    for i in range(4):
        ids.append(await queue.enqueue(_spec(f"n{i}", tenant_id=None), now=BASE))

    claimed = await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    assert sorted(c.id for c in claimed) == sorted(ids)  # all 4 claimed


async def test_tenant_with_live_claim_excluded_from_second_batch() -> None:
    queue = InMemoryBackgroundJobQueue()
    await queue.enqueue(_spec("a0", tenant_id="tenant-a"), now=BASE)
    await queue.enqueue(_spec("a1", tenant_id="tenant-a"), now=BASE)

    first = await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    assert [c.character_id for c in first] == ["a0"]  # one claimed, lease live

    # A second worker within the lease window sees NO claimable tenant-a job:
    # the tenant is capped by its live claim (a1 excluded).
    second = await queue.claim(
        "w2", now=BASE + timedelta(seconds=10), limit=10, lease_seconds=60,
    )
    assert second == []

    # Once the first lease expires, the tenant frees up and a1 becomes claimable.
    later = BASE + timedelta(seconds=61)
    third = await queue.claim("w2", now=later, limit=10, lease_seconds=60)
    # a0's lease also expired → reclaimable; fairness keeps one per tenant, so
    # exactly one tenant-a job comes back this batch.
    assert len(third) == 1
    assert third[0].tenant_id == "tenant-a"


async def test_extra_same_tenant_job_stays_queued_for_next_round() -> None:
    queue = InMemoryBackgroundJobQueue()
    await queue.enqueue(_spec("a0", tenant_id="tenant-a"), now=BASE)
    await queue.enqueue(_spec("a1", tenant_id="tenant-a"), now=BASE)

    first = await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    assert len(first) == 1
    # The un-claimed extra was NOT mutated — complete the first, then the extra
    # is claimable next round (no live claim caps the tenant anymore).
    from kokoro_link.contracts.background_jobs import JobOutcome

    assert await queue.complete(
        first[0].id, "w1", outcome=JobOutcome(), now=BASE,
    )
    second = await queue.claim(
        "w1", now=BASE + timedelta(seconds=1), limit=10, lease_seconds=60,
    )
    assert len(second) == 1
    assert second[0].character_id == "a1"
