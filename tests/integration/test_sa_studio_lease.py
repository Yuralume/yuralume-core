"""PostgreSQL integration for the per-target Studio execution lease.

HOSTED_CORE_SCALING_ARCHITECTURE_PLAN §13 Phase 4 前置 1.

Proves the cross-replica guarantees against the REAL SA lease on the shared
``background_runtime_leases`` table (via ``StudioExecutionLease`` under
``studio:<target_id>`` names):

* two simulated api replicas racing ``acquire`` on one target → exactly one wins;
* two replicas' startup recovery of the same running job → exactly one re-drives,
  the other records a ``lease_skipped`` and leaves the job row untouched;
* a lease that lapses is taken over by the other replica (epoch bumps) and the
  stale owner's heartbeat then fails — the signal a live runner uses to abort.

All time is relative to a fixed base instant. Requires Docker (testcontainers);
skipped automatically when unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.studio_execution_lease import (
    StudioExecutionLease,
)
from kokoro_link.application.services.studio_job_recovery import (
    StudioJobRecoveryService,
)
from kokoro_link.contracts.studio_jobs import (
    JOB_KIND_FUSION_CREATE,
    JOB_STATUS_RUNNING,
    StudioGenerationJob,
)
from kokoro_link.infrastructure.persistence.sa_background_runtime import (
    SABackgroundCoordinatorLease,
)
from kokoro_link.infrastructure.repositories.in_memory_studio_jobs import (
    InMemoryStudioJobRepository,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


class _FakeFusion:
    """Records re-drive dispatches without running a real pipeline."""

    def __init__(self) -> None:
        self.resume_calls: list[str] = []

    async def resume_job(self, job: StudioGenerationJob) -> str:
        self.resume_calls.append(job.id)
        return "resumed"


async def test_two_replicas_only_one_acquires(session_factory) -> None:
    backend = SABackgroundCoordinatorLease(session_factory)
    a = StudioExecutionLease(backend, owner_id="replica-A", ttl_seconds=100)
    b = StudioExecutionLease(backend, owner_id="replica-B", ttl_seconds=100)

    assert await a.acquire("story-1", now=BASE) == 1
    # A live lease held by A denies B on the same target.
    assert await b.acquire("story-1", now=BASE + timedelta(seconds=1)) is None
    # A can still heartbeat (same epoch).
    assert await a.heartbeat(
        "story-1", 1, now=BASE + timedelta(seconds=2),
    ) is True
    # A different target is independent.
    assert await b.acquire("story-2", now=BASE) == 1


async def test_expired_lease_taken_over_and_stale_heartbeat_fails(
    session_factory,
) -> None:
    backend = SABackgroundCoordinatorLease(session_factory)
    a = StudioExecutionLease(backend, owner_id="A", ttl_seconds=100)
    b = StudioExecutionLease(backend, owner_id="B", ttl_seconds=100)

    assert await a.acquire("story-x", now=BASE) == 1
    after = BASE + timedelta(seconds=101)
    # B takes over the lapsed lease — ownership change bumps the epoch.
    assert await b.acquire("story-x", now=after) == 2
    # A's heartbeat now fails: it is no longer the owner (the runner abort cue).
    assert await a.heartbeat(
        "story-x", 1, now=after + timedelta(seconds=1),
    ) is False


async def test_two_replica_recovery_redrives_once(session_factory) -> None:
    jobs = InMemoryStudioJobRepository()
    fusion = _FakeFusion()
    job = StudioGenerationJob.create(
        kind=JOB_KIND_FUSION_CREATE, target_id="story-r", params={},
    )
    await jobs.add(job)

    backend = SABackgroundCoordinatorLease(session_factory)
    lease_a = StudioExecutionLease(backend, owner_id="A", ttl_seconds=300)
    lease_b = StudioExecutionLease(backend, owner_id="B", ttl_seconds=300)
    rec_a = StudioJobRecoveryService(
        jobs=jobs, fusion_story_service=fusion, execution_lease=lease_a,  # type: ignore[arg-type]
    )
    rec_b = StudioJobRecoveryService(
        jobs=jobs, fusion_story_service=fusion, execution_lease=lease_b,  # type: ignore[arg-type]
    )

    # A recovers first and claims the target's lease (kept for the "resumed"
    # runner); B recovers and must skip the same target.
    report_a = await rec_a.recover()
    report_b = await rec_b.recover()

    assert report_a["resumed"] + report_b["resumed"] == 1
    assert report_a["lease_skipped"] + report_b["lease_skipped"] == 1
    # The pipeline was dispatched exactly once — no double-drive.
    assert len(fusion.resume_calls) == 1
    # The skipping replica left the job row untouched (still RUNNING).
    remaining = await jobs.get(job.id)
    assert remaining is not None and remaining.status == JOB_STATUS_RUNNING


async def test_recovery_release_frees_finalized_target(session_factory) -> None:
    """A target finalized synchronously (no runner) releases its lease, so a
    later recovery pass can re-claim it rather than wait for the TTL."""
    jobs = InMemoryStudioJobRepository()

    class _FinalizingFusion:
        def __init__(self) -> None:
            self.calls = 0

        async def resume_job(self, job: StudioGenerationJob) -> str:
            self.calls += 1
            return "finalized"

    fusion = _FinalizingFusion()
    job = StudioGenerationJob.create(
        kind=JOB_KIND_FUSION_CREATE, target_id="story-f", params={},
    )
    await jobs.add(job)

    backend = SABackgroundCoordinatorLease(session_factory)
    lease = StudioExecutionLease(backend, owner_id="A", ttl_seconds=300)
    rec = StudioJobRecoveryService(
        jobs=jobs, fusion_story_service=fusion, execution_lease=lease,  # type: ignore[arg-type]
    )
    await rec.recover()

    # "finalized" (no runner owns the lease) → recovery released it: the target
    # is immediately re-acquirable rather than pinned for the whole TTL.
    assert await lease.acquire("story-f", now=BASE) is not None
