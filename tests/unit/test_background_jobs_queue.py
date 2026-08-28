"""Unit tests for the in-memory background-job queue semantics.

These lock the contract every adapter must satisfy (§14): idempotency,
claim ordering, lease expiry / reclaim, ownership checks, bounded retry →
dead, redrive, retention split, stats, and error sanitisation.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    BackgroundJobSpec,
    JobOutcome,
    JobStatus,
)
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
    InMemoryBackgroundJobQueue,
)


pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def _spec(
    *,
    kind: str = "character_tick",
    key: str = "char-1:2026-07-20",
    due_at: datetime = BASE,
    priority: int = 3,
    epoch: int = 0,
    max_attempts: int = 5,
    # Queue-mechanics tests here exercise ordering / lease / retry, NOT the P3-C
    # per-tenant fairness cap (a tenant gets at most one job per claim batch).
    # Default to a tenant-exempt (None) job so these tests keep validating batch
    # mechanics unperturbed; fairness has its own dedicated tests with explicit
    # tenant ids (test_background_jobs_tenant_fairness).
    tenant_id: str | None = None,
    character_id: str | None = "char-1",
) -> BackgroundJobSpec:
    return BackgroundJobSpec(
        kind=kind,
        idempotency_key=key,
        due_at=due_at,
        fencing_epoch=epoch,
        priority=priority,
        max_attempts=max_attempts,
        tenant_id=tenant_id,
        character_id=character_id,
        payload={"character_id": character_id or ""},
    )


async def test_enqueue_is_idempotent_while_active_and_recurs_after_done() -> None:
    queue = InMemoryBackgroundJobQueue()
    first = await queue.enqueue(_spec(), now=BASE)
    second = await queue.enqueue(_spec(), now=BASE)

    assert first is not None
    assert second is None  # active key already present — idempotent, not error

    claimed = await queue.claim(
        "w1", now=BASE, limit=10, lease_seconds=60,
    )
    assert [c.id for c in claimed] == [first]
    ok = await queue.complete(
        first, "w1", outcome=JobOutcome(detail={"ran": True}), now=BASE,
    )
    assert ok

    # Same logical key may recur once the previous occurrence is terminal.
    third = await queue.enqueue(_spec(), now=BASE)
    assert third is not None and third != first


async def test_claim_orders_by_priority_then_due_at() -> None:
    queue = InMemoryBackgroundJobQueue()
    late_high = await queue.enqueue(
        _spec(key="a", priority=1, due_at=BASE + timedelta(seconds=30)), now=BASE,
    )
    early_high = await queue.enqueue(
        _spec(key="b", priority=1, due_at=BASE), now=BASE,
    )
    low = await queue.enqueue(_spec(key="c", priority=5, due_at=BASE), now=BASE)

    claimed = await queue.claim("w1", now=BASE + timedelta(minutes=1), limit=10, lease_seconds=60)
    assert [c.id for c in claimed] == [early_high, late_high, low]


async def test_claim_excludes_future_due_and_non_queued() -> None:
    queue = InMemoryBackgroundJobQueue()
    ready = await queue.enqueue(_spec(key="ready", due_at=BASE), now=BASE)
    await queue.enqueue(_spec(key="future", due_at=BASE + timedelta(hours=1)), now=BASE)

    claimed = await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    assert [c.id for c in claimed] == [ready]

    # The claimed one is no longer visible to a second claim within lease.
    again = await queue.claim("w2", now=BASE, limit=10, lease_seconds=60)
    assert again == []


async def test_expired_lease_is_reclaimable_and_charges_attempt() -> None:
    queue = InMemoryBackgroundJobQueue()
    job_id = await queue.enqueue(_spec(), now=BASE)

    first = await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    assert [c.id for c in first] == [job_id]
    assert first[0].attempt_count == 1

    # Before lease expiry: not reclaimable.
    assert await queue.claim(
        "w2", now=BASE + timedelta(seconds=30), limit=10, lease_seconds=60,
    ) == []

    # After lease expiry: another worker reclaims, attempt charged again.
    after = BASE + timedelta(seconds=61)
    reclaimed = await queue.claim("w2", now=after, limit=10, lease_seconds=60)
    assert [c.id for c in reclaimed] == [job_id]
    assert reclaimed[0].attempt_count == 2
    assert reclaimed[0].lease_owner == "w2"

    # The stale first owner can no longer complete it.
    assert not await queue.complete(
        job_id, "w1", outcome=JobOutcome(), now=after,
    )


async def test_complete_and_fail_require_valid_lease_ownership() -> None:
    queue = InMemoryBackgroundJobQueue()
    job_id = await queue.enqueue(_spec(), now=BASE)
    await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)

    # Wrong worker: no completion, no state change.
    assert not await queue.complete(
        job_id, "intruder", outcome=JobOutcome(), now=BASE,
    )
    assert not await queue.fail(
        job_id, "intruder", error="nope", now=BASE, retry_in_seconds=None,
    )
    job = await queue.get(job_id)
    assert job is not None and job.status == JobStatus.CLAIMED

    # Expired lease: even the right worker fails the guard.
    assert not await queue.complete(
        job_id, "w1", outcome=JobOutcome(), now=BASE + timedelta(seconds=61),
    )


async def test_extend_lease_ownership_check() -> None:
    queue = InMemoryBackgroundJobQueue()
    job_id = await queue.enqueue(_spec(), now=BASE)
    await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)

    assert not await queue.extend_lease(
        job_id, "intruder", until=BASE + timedelta(minutes=5), now=BASE,
    )
    assert await queue.extend_lease(
        job_id, "w1", until=BASE + timedelta(minutes=5), now=BASE,
    )
    # Now the job survives past the original lease window.
    assert await queue.claim(
        "w2", now=BASE + timedelta(seconds=61), limit=10, lease_seconds=60,
    ) == []


async def test_extend_lease_rejected_after_expiry() -> None:
    # H2(c) Codex repro: once the lease has expired, even the current owner
    # cannot ``extend_lease`` — the job must be re-claimed (which charges an
    # attempt), never silently revived by a late heartbeat.
    queue = InMemoryBackgroundJobQueue()
    job_id = await queue.enqueue(_spec(), now=BASE)
    await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)

    # Still valid inside the lease window.
    assert await queue.extend_lease(
        job_id, "w1", until=BASE + timedelta(minutes=5),
        now=BASE + timedelta(seconds=30),
    )
    # Past the (now-extended) lease_until: expired → not extendable.
    assert not await queue.extend_lease(
        job_id, "w1", until=BASE + timedelta(minutes=10),
        now=BASE + timedelta(minutes=6),
    )


async def test_release_claim_requeues_immediately_and_refunds_the_attempt() -> None:
    # GD0 graceful stop: an unstarted claim handed back is instantly re-claimable
    # by another worker — no waiting out the lease — and costs no attempt.
    queue = InMemoryBackgroundJobQueue()
    job_id = await queue.enqueue(_spec(), now=BASE)
    claimed = await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    assert claimed[0].attempt_count == 1

    assert await queue.release_claim(job_id, "w1", now=BASE)

    job = await queue.get(job_id)
    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert job.attempt_count == 0
    assert job.lease_owner is None and job.lease_until is None
    assert job.claimed_at is None
    assert job.due_at == BASE  # ready now, not deferred

    # A surviving worker picks it up on the very next pass, still inside what
    # would have been the original lease window.
    reclaimed = await queue.claim(
        "w2", now=BASE + timedelta(seconds=1), limit=10, lease_seconds=60,
    )
    assert [c.id for c in reclaimed] == [job_id]
    assert reclaimed[0].attempt_count == 1


async def test_withdraw_queued_retires_the_row_and_frees_the_key() -> None:
    # TU4: turn-undo deletes the follow-up row a release job was scheduled
    # to fire, so the job now describes work that no longer exists.
    queue = InMemoryBackgroundJobQueue()
    job_id = await queue.enqueue(_spec(key="follow_up:row-1:900"), now=BASE)

    assert await queue.withdraw_queued("follow_up:row-1:900", now=BASE) == 1

    job = await queue.get(job_id)
    assert job is not None
    assert job.status == JobStatus.SUPERSEDED
    assert job.finished_at == BASE
    # Terminal, so the same logical key may recur immediately rather than
    # staying blocked until the withdrawn job's original due instant.
    assert await queue.enqueue(_spec(key="follow_up:row-1:900"), now=BASE)
    # And a second withdrawal of an already-retired key is a no-op count,
    # not a double retirement of the fresh occurrence's predecessor.
    assert await queue.withdraw_queued("nothing-here", now=BASE) == 0


async def test_withdraw_queued_leaves_a_claimed_job_to_its_worker() -> None:
    # A worker holds the lease and is the only party allowed to end that
    # job; its own subject re-verification is the guard there.
    queue = InMemoryBackgroundJobQueue()
    job_id = await queue.enqueue(_spec(key="follow_up:row-2:900"), now=BASE)
    await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)

    assert await queue.withdraw_queued("follow_up:row-2:900", now=BASE) == 0

    job = await queue.get(job_id)
    assert job is not None
    assert job.status == JobStatus.CLAIMED
    assert job.lease_owner == "w1"


async def test_release_claim_requires_valid_lease_ownership() -> None:
    queue = InMemoryBackgroundJobQueue()
    job_id = await queue.enqueue(_spec(), now=BASE)
    await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)

    # Wrong worker: refused, no state change.
    assert not await queue.release_claim(job_id, "intruder", now=BASE)
    job = await queue.get(job_id)
    assert job is not None and job.status == JobStatus.CLAIMED

    # Expired lease: another worker may already have reclaimed it, so the
    # former owner must not requeue it out from under them.
    after = BASE + timedelta(seconds=61)
    reclaimed = await queue.claim("w2", now=after, limit=10, lease_seconds=60)
    assert [c.id for c in reclaimed] == [job_id]
    assert not await queue.release_claim(job_id, "w1", now=after)
    job = await queue.get(job_id)
    assert job is not None
    assert job.status == JobStatus.CLAIMED and job.lease_owner == "w2"


async def test_retry_backoff_reaches_dead_at_max_attempts() -> None:
    queue = InMemoryBackgroundJobQueue()
    job_id = await queue.enqueue(_spec(max_attempts=2), now=BASE)

    await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    assert await queue.fail(
        job_id, "w1", error="boom", now=BASE, retry_in_seconds=0,
    )
    job = await queue.get(job_id)
    assert job is not None and job.status == JobStatus.QUEUED

    await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    assert await queue.fail(
        job_id, "w1", error="boom again", now=BASE, retry_in_seconds=0,
    )
    job = await queue.get(job_id)
    assert job is not None and job.status == JobStatus.DEAD

    dead = await queue.list_dead(limit=10)
    assert [d.id for d in dead] == [job_id]


async def test_fail_without_retry_goes_failed() -> None:
    queue = InMemoryBackgroundJobQueue()
    job_id = await queue.enqueue(_spec(), now=BASE)
    await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    assert await queue.fail(job_id, "w1", error="stop", now=BASE)
    job = await queue.get(job_id)
    assert job is not None and job.status == JobStatus.FAILED
    assert job.finished_at is not None


async def test_redrive_resets_dead_job() -> None:
    queue = InMemoryBackgroundJobQueue()
    job_id = await queue.enqueue(_spec(max_attempts=1), now=BASE)
    await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    await queue.fail(job_id, "w1", error="boom", now=BASE)
    assert (await queue.get(job_id)).status == JobStatus.DEAD

    assert await queue.redrive(job_id)
    job = await queue.get(job_id)
    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert job.attempt_count == 0
    assert job.lease_owner is None
    assert job.last_error is None

    # A non-dead job cannot be redriven.
    assert not await queue.redrive(job_id)


async def test_redrive_refused_when_active_key_already_exists() -> None:
    # The logical key recurred while the first occurrence sat dead: a fresh
    # active row now holds it. Reviving the dead row would create a SECOND
    # active row for the key — the SA partial index rejects that, and the
    # in-memory adapter must refuse identically (parity), returning False and
    # leaving the dead row dead rather than silently double-activating the key.
    queue = InMemoryBackgroundJobQueue()
    dead_id = await queue.enqueue(_spec(key="recurring", max_attempts=1), now=BASE)
    await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    await queue.fail(dead_id, "w1", error="boom", now=BASE)
    assert (await queue.get(dead_id)).status == JobStatus.DEAD

    # A fresh occurrence of the same logical key (allowed — dead is terminal).
    fresh_id = await queue.enqueue(_spec(key="recurring"), now=BASE)
    assert fresh_id is not None and fresh_id != dead_id

    assert await queue.redrive(dead_id) is False
    assert (await queue.get(dead_id)).status == JobStatus.DEAD  # untouched
    assert (await queue.get(fresh_id)).status == JobStatus.QUEUED


async def test_prune_honors_7d_terminal_and_30d_dead_split() -> None:
    queue = InMemoryBackgroundJobQueue()

    done_id = await queue.enqueue(_spec(key="done"), now=BASE)
    await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    await queue.complete(done_id, "w1", outcome=JobOutcome(), now=BASE)

    dead_id = await queue.enqueue(_spec(key="dead", max_attempts=1), now=BASE)
    await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    await queue.fail(dead_id, "w1", error="boom", now=BASE)

    # 8 days later: done pruned, dead survives (30-day clock).
    removed = await queue.prune(now=BASE + timedelta(days=8))
    assert removed == 1
    assert await queue.get(done_id) is None
    assert (await queue.get(dead_id)).status == JobStatus.DEAD

    # 31 days later: dead pruned too.
    removed = await queue.prune(now=BASE + timedelta(days=31))
    assert removed == 1
    assert await queue.get(dead_id) is None


async def test_stats_reports_counts_and_oldest_age() -> None:
    queue = InMemoryBackgroundJobQueue()
    await queue.enqueue(_spec(kind="character_tick", key="a", due_at=BASE), now=BASE)
    await queue.enqueue(
        _spec(kind="social_tick", key="b", due_at=BASE + timedelta(seconds=10)), now=BASE,
    )
    claimed_id = await queue.enqueue(
        _spec(kind="character_tick", key="c", due_at=BASE), now=BASE,
    )
    await queue.claim("w1", now=BASE, limit=1, lease_seconds=60)

    now = BASE + timedelta(seconds=120)
    stats = await queue.stats(now=now)
    assert stats.total == 3
    assert stats.status_counts.get("queued") == 2
    assert stats.status_counts.get("claimed") == 1
    assert stats.kind_counts.get("character_tick") == 2
    assert stats.kind_counts.get("social_tick") == 1
    # Oldest ready queued job is due at BASE (120s ago). claimed_id was the
    # one claimed (priority tie → due_at tie → insertion is deterministic),
    # but at least one BASE-due queued row remains.
    assert stats.oldest_queued_age_seconds == pytest.approx(120.0)
    assert claimed_id is not None


async def test_fail_sanitizes_error_and_never_stores_secret() -> None:
    queue = InMemoryBackgroundJobQueue()
    job_id = await queue.enqueue(_spec(), now=BASE)
    await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)

    secret = "sk-" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4"
    await queue.fail(
        job_id, "w1",
        error=RuntimeError(f"upstream 401 with token {secret}"),
        now=BASE,
    )
    job = await queue.get(job_id)
    assert job is not None
    assert job.last_error  # never empty
    assert secret not in job.last_error
    assert "[redacted]" in job.last_error
    assert "RuntimeError" in job.last_error


async def test_enqueue_fences_against_coordinator_epoch() -> None:
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)

    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord-a", ttl_seconds=30, now=BASE,
    )
    assert epoch == 1

    # Correct epoch + live lease enqueues.
    assert await queue.enqueue(_spec(epoch=1, key="ok"), now=BASE) is not None
    # Stale epoch (ex-coordinator) is silently rejected.
    assert await queue.enqueue(_spec(epoch=0, key="stale"), now=BASE) is None


async def test_enqueue_rejected_when_coordinator_lease_expired() -> None:
    # H1 liveness half: even with the RIGHT epoch, an owner whose own lease has
    # expired at ``now`` is no longer the live leader and cannot enqueue — it
    # must re-acquire (which bumps the epoch) first.
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    assert await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord-a", ttl_seconds=30, now=BASE,
    ) == 1

    # Live at now inside the lease window.
    assert await queue.enqueue(
        _spec(epoch=1, key="live"), now=BASE + timedelta(seconds=10),
    ) is not None
    # Expired at now (lease_until BASE+30 < BASE+31) → rejected despite epoch 1.
    assert await queue.enqueue(
        _spec(epoch=1, key="expired"), now=BASE + timedelta(seconds=31),
    ) is None
