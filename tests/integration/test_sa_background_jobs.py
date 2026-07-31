"""PostgreSQL integration tests for the background-queue adapters (§14).

Exercises the behaviours that only a real database can prove: atomic
concurrent ``SKIP LOCKED`` claims, the DB-enforced idempotency
constraint under concurrent enqueue, lease takeover + epoch fencing,
expired-lease completion rejection, the retry→dead→redrive round trip,
and journal/cursor round trips. Requires Docker (testcontainers); skipped
automatically when Docker is unavailable (see tests/conftest.py).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    BackgroundJobSpec,
    JobOutcome,
    JobStatus,
)
from kokoro_link.infrastructure.persistence.models import (
    BackgroundJobRow,
    BackgroundRuntimeLeaseRow,
)
from kokoro_link.infrastructure.persistence.sa_background_jobs import (
    SABackgroundJobQueue,
)
from kokoro_link.infrastructure.persistence.sa_background_runtime import (
    SABackgroundCoordinatorCursor,
    SABackgroundCoordinatorLease,
    SATickJournal,
)


pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def _spec(key: str, *, epoch: int, due_at: datetime = BASE, **kw) -> BackgroundJobSpec:
    return BackgroundJobSpec(
        kind=kw.pop("kind", "character_tick"),
        idempotency_key=key,
        due_at=due_at,
        fencing_epoch=epoch,
        # Default tenant-exempt (None) so the concurrency / ordering / lease
        # mechanics tests here are NOT subject to the P3-C per-tenant fairness
        # cap (one claimed job per tenant cluster-wide); the fairness test sets
        # explicit tenant ids. A test asserting tenant round-trip passes it too.
        tenant_id=kw.pop("tenant_id", None),
        character_id=kw.pop("character_id", "char-1"),
        payload=kw.pop("payload", {"character_id": "char-1"}),
        **kw,
    )


async def _acquire_coordinator(session_factory, *, now: datetime = BASE) -> int:
    lease = SABackgroundCoordinatorLease(session_factory)
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=300, now=now,
    )
    assert epoch is not None
    return epoch


async def _count_rows(session_factory) -> int:
    async with session_factory() as session:
        return int(
            (await session.execute(
                select(func.count()).select_from(BackgroundJobRow),
            )).scalar_one(),
        )


async def test_concurrent_claims_are_disjoint_and_complete(session_factory) -> None:
    epoch = await _acquire_coordinator(session_factory)
    queue = SABackgroundJobQueue(session_factory)
    total = 20
    for i in range(total):
        assert await queue.enqueue(_spec(f"job-{i}", epoch=epoch), now=BASE) is not None

    async def drain(worker_id: str) -> list[str]:
        got: list[str] = []
        while True:
            claimed = await queue.claim(
                worker_id, now=BASE, limit=5, lease_seconds=60,
            )
            if not claimed:
                break
            got.extend(c.id for c in claimed)
            await asyncio.sleep(0)  # yield so the other worker contends
        return got

    a_ids, b_ids = await asyncio.gather(drain("w-a"), drain("w-b"))

    assert set(a_ids).isdisjoint(b_ids)  # no row claimed twice
    assert set(a_ids) | set(b_ids)  # both got some / all covered
    assert len(set(a_ids) | set(b_ids)) == total  # union complete

    # Every claimed job was charged exactly one attempt (never double).
    for job_id in a_ids + b_ids:
        job = await queue.get(job_id)
        assert job is not None and job.attempt_count == 1
        assert await queue.complete(
            job_id,
            job.lease_owner or "",
            outcome=JobOutcome(detail={"ok": True}),
            now=BASE,
        )


async def test_double_enqueue_same_active_key_keeps_one_row(session_factory) -> None:
    epoch = await _acquire_coordinator(session_factory)
    queue = SABackgroundJobQueue(session_factory)

    # Sequential: second active enqueue is idempotent None.
    first = await queue.enqueue(_spec("dupe", epoch=epoch), now=BASE)
    second = await queue.enqueue(_spec("dupe", epoch=epoch), now=BASE)
    assert first is not None
    assert second is None
    assert await _count_rows(session_factory) == 1

    # Concurrent: the partial-unique constraint itself fires — exactly one
    # of the racing inserts commits, the other surfaces as idempotent None.
    results = await asyncio.gather(
        queue.enqueue(_spec("race", epoch=epoch), now=BASE),
        queue.enqueue(_spec("race", epoch=epoch), now=BASE),
        queue.enqueue(_spec("race", epoch=epoch), now=BASE),
    )
    assert sum(1 for r in results if r is not None) == 1
    async with session_factory() as session:
        race_rows = int((await session.execute(
            select(func.count()).select_from(BackgroundJobRow)
            .where(BackgroundJobRow.idempotency_key == "race"),
        )).scalar_one())
    assert race_rows == 1


async def test_lease_takeover_bumps_epoch_and_fences_stale_enqueue(
    session_factory,
) -> None:
    lease = SABackgroundCoordinatorLease(session_factory)
    queue = SABackgroundJobQueue(session_factory)

    e1 = await lease.acquire(
        COORDINATOR_LEASE_NAME, "A", ttl_seconds=1, now=BASE,
    )
    assert e1 == 1
    assert await queue.enqueue(_spec("owned-by-a", epoch=1), now=BASE) is not None

    # A's lease expires; B takes over → epoch 2.
    later = BASE + timedelta(seconds=5)
    e2 = await lease.acquire(
        COORDINATOR_LEASE_NAME, "B", ttl_seconds=300, now=later,
    )
    assert e2 == 2
    assert await lease.renew(
        COORDINATOR_LEASE_NAME, "A", ttl_seconds=1, now=later,
    ) is None

    # The partitioned ex-coordinator (stale epoch 1) cannot insert.
    before = await _count_rows(session_factory)
    assert await queue.enqueue(_spec("stale", epoch=1), now=BASE) is None
    assert await _count_rows(session_factory) == before

    # The current coordinator (epoch 2) can.
    assert await queue.enqueue(_spec("fresh", epoch=2), now=BASE) is not None


async def test_enqueue_fences_atomically_against_concurrent_takeover(
    session_factory,
) -> None:
    # H1: the fencing read must be ATOMIC against a concurrent takeover. We hold
    # the lease row lock (FOR UPDATE) in a separate session with an UNCOMMITTED
    # epoch bump — a takeover in flight — then race a stale (epoch-1) enqueue.
    #
    # A plain ``session.get`` (pre-fix) reads the last COMMITTED epoch (still 1)
    # and inserts a stale row. The fixed ``SELECT ... FOR UPDATE`` instead BLOCKS
    # on the held lock; once the takeover commits epoch 2, the enqueue re-reads
    # epoch 2 under the lock and rejects. Deterministic: the DB lock enforces the
    # ordering, so the outcome distinguishes fixed from broken.
    lease = SABackgroundCoordinatorLease(session_factory)
    queue = SABackgroundJobQueue(session_factory)
    assert await lease.acquire(
        COORDINATOR_LEASE_NAME, "A", ttl_seconds=300, now=BASE,
    ) == 1

    async with session_factory() as taker:
        row = await taker.get(
            BackgroundRuntimeLeaseRow, COORDINATOR_LEASE_NAME,
            with_for_update=True,
        )
        row.epoch = 2
        row.owner_id = "B"
        row.lease_until = BASE + timedelta(seconds=300)
        await taker.flush()  # hold the row lock, uncommitted

        enqueue_task = asyncio.create_task(
            queue.enqueue(_spec("stale-race", epoch=1), now=BASE),
        )
        # Give the enqueue time to reach SELECT ... FOR UPDATE and block on the
        # held lock. With atomic fencing it MUST still be pending here.
        await asyncio.sleep(0.3)
        assert not enqueue_task.done(), (
            "enqueue did not block on the lease lock — fencing is not atomic"
        )
        await taker.commit()  # takeover commits epoch 2

    result = await enqueue_task
    assert result is None  # stale epoch observed under the lock → rejected
    async with session_factory() as session:
        stale_rows = int((await session.execute(
            select(func.count()).select_from(BackgroundJobRow)
            .where(BackgroundJobRow.idempotency_key == "stale-race"),
        )).scalar_one())
    assert stale_rows == 0  # zero stale rows


async def test_renew_and_extend_reject_after_expiry(session_factory) -> None:
    # H2 PG: the lease/queue expiry guards hold against a real database too.
    lease = SABackgroundCoordinatorLease(session_factory)
    queue = SABackgroundJobQueue(session_factory)
    assert await lease.acquire(
        COORDINATOR_LEASE_NAME, "A", ttl_seconds=1, now=BASE,
    ) == 1
    expired = BASE + timedelta(seconds=5)
    # renew after expiry → None even for the same owner (must re-acquire).
    assert await lease.renew(
        COORDINATOR_LEASE_NAME, "A", ttl_seconds=1, now=expired,
    ) is None
    # same-owner re-acquire over the expired lease bumps the epoch (new
    # incarnation), so its enqueues carry the NEW epoch.
    assert await lease.acquire(
        COORDINATOR_LEASE_NAME, "A", ttl_seconds=300, now=expired,
    ) == 2

    job_id = await queue.enqueue(_spec("j", epoch=2), now=expired)
    assert job_id is not None
    await queue.claim("A", now=expired, limit=10, lease_seconds=1)
    # extend after the job lease expired → False (not extendable, must reclaim).
    later = expired + timedelta(seconds=5)
    assert not await queue.extend_lease(
        job_id, "A", until=later + timedelta(seconds=60), now=later,
    )


async def test_complete_with_expired_lease_fails_then_reclaimable(
    session_factory,
) -> None:
    epoch = await _acquire_coordinator(session_factory)
    queue = SABackgroundJobQueue(session_factory)
    job_id = await queue.enqueue(_spec("job", epoch=epoch), now=BASE)
    assert job_id is not None

    claimed = await queue.claim("A", now=BASE, limit=10, lease_seconds=1)
    assert [c.id for c in claimed] == [job_id]

    expired = BASE + timedelta(seconds=5)
    # A's lease has expired → complete is rejected.
    assert not await queue.complete(
        job_id, "A", outcome=JobOutcome(), now=expired,
    )
    # And the job is reclaimable by another worker, charging attempt 2.
    reclaimed = await queue.claim("B", now=expired, limit=10, lease_seconds=60)
    assert [c.id for c in reclaimed] == [job_id]
    assert reclaimed[0].attempt_count == 2


async def test_release_claim_requeues_refunds_and_guards_ownership(
    session_factory,
) -> None:
    # GD0 graceful stop, SA parity with the in-memory adapter: an unstarted
    # claim goes straight back to ``queued`` (attempt refunded, ready now), and
    # a stale owner cannot requeue a row another worker has since reclaimed.
    epoch = await _acquire_coordinator(session_factory)
    queue = SABackgroundJobQueue(session_factory)
    job_id = await queue.enqueue(_spec("job", epoch=epoch), now=BASE)
    assert job_id is not None

    claimed = await queue.claim("A", now=BASE, limit=10, lease_seconds=60)
    assert [c.id for c in claimed] == [job_id]
    assert claimed[0].attempt_count == 1

    # Wrong worker is refused, row untouched.
    assert not await queue.release_claim(job_id, "intruder", now=BASE)
    job = await queue.get(job_id)
    assert job is not None and job.status == JobStatus.CLAIMED

    assert await queue.release_claim(job_id, "A", now=BASE)
    job = await queue.get(job_id)
    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert job.attempt_count == 0
    assert job.lease_owner is None and job.lease_until is None
    assert job.claimed_at is None

    # Immediately re-claimable — no waiting out the original lease.
    reclaimed = await queue.claim(
        "B", now=BASE + timedelta(seconds=1), limit=10, lease_seconds=60,
    )
    assert [c.id for c in reclaimed] == [job_id]
    assert reclaimed[0].attempt_count == 1

    # The former owner's late release must not clobber B's live claim.
    assert not await queue.release_claim(
        job_id, "A", now=BASE + timedelta(seconds=2),
    )
    job = await queue.get(job_id)
    assert job is not None
    assert job.status == JobStatus.CLAIMED and job.lease_owner == "B"


async def test_retry_dead_redrive_round_trip(session_factory) -> None:
    epoch = await _acquire_coordinator(session_factory)
    queue = SABackgroundJobQueue(session_factory)
    job_id = await queue.enqueue(_spec("job", epoch=epoch, max_attempts=1), now=BASE)
    assert job_id is not None

    await queue.claim("A", now=BASE, limit=10, lease_seconds=60)
    # attempt_count == max_attempts (1) → dead regardless of retry.
    assert await queue.fail(job_id, "A", error=RuntimeError("boom"), now=BASE)

    job = await queue.get(job_id)
    assert job is not None and job.status == JobStatus.DEAD
    assert job.last_error and "RuntimeError" in job.last_error

    dead = await queue.list_dead(limit=10)
    assert [d.id for d in dead] == [job_id]

    assert await queue.redrive(job_id)
    job = await queue.get(job_id)
    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert job.attempt_count == 0
    assert job.last_error is None

    # Redriven job is claimable again.
    again = await queue.claim("A", now=BASE, limit=10, lease_seconds=60)
    assert [c.id for c in again] == [job_id]


async def test_fail_never_clobbers_a_concurrent_reclaim(session_factory) -> None:
    # Owner A's lease is still valid at A's clock (now_A) while worker B's clock
    # (now_B) has crossed the expiry, so B may reclaim the same row. fail() must
    # take the row lock and re-validate ownership inside it (like complete),
    # never PK-blind-writing over B's reclaim. The safe outcomes are: B holds it
    # (CLAIMED, attempt 2) OR A re-queued it (QUEUED, attempt 1, no owner) — but
    # NEVER attempt 2 while QUEUED/unowned (the half-applied clobber). Looped to
    # exercise multiple interleavings; the fixed code satisfies the invariant on
    # every one.
    for i in range(12):
        queue = SABackgroundJobQueue(session_factory)
        epoch = await _acquire_coordinator(session_factory, now=BASE)
        key = f"racer-{i}"
        job_id = await queue.enqueue(_spec(key, epoch=epoch), now=BASE)
        assert job_id is not None

        # A claims with a 1s lease → valid at now_A=BASE, expired at now_B.
        claimed = await queue.claim("A", now=BASE, limit=10, lease_seconds=1)
        assert [c.id for c in claimed] == [job_id]

        now_b = BASE + timedelta(seconds=2)
        fail_res, reclaim = await asyncio.gather(
            queue.fail(
                job_id, "A", error="stale", now=BASE, retry_in_seconds=0,
            ),
            queue.claim("B", now=now_b, limit=10, lease_seconds=60),
        )

        job = await queue.get(job_id)
        assert job is not None
        # The core safety invariant: a reclaim (attempt 2) is never left as a
        # queued/unowned row — that would let a THIRD worker run it while B is.
        if job.attempt_count >= 2:
            assert job.status == JobStatus.CLAIMED
            assert job.lease_owner == "B"
        assert not (
            job.status == JobStatus.QUEUED and job.lease_owner is not None
        )

        # Clean the row so the next iteration's idempotency key is fresh.
        async with session_factory() as session:
            await session.execute(
                BackgroundJobRow.__table__.delete().where(
                    BackgroundJobRow.id == job_id,
                ),
            )
            await session.commit()


async def test_concurrent_first_acquire_no_integrity_error(
    session_factory,
) -> None:
    # Two schedulers cold-starting race to create the SAME lease row (FOR UPDATE
    # cannot gap-lock a not-yet-existent PK). Neither acquire() may leak the PK
    # IntegrityError: exactly one wins epoch 1, the other reads the live lease
    # and returns None (passive) — no exception, one row.
    lease = SABackgroundCoordinatorLease(session_factory)
    name = "race-lease-first-acquire"
    results = await asyncio.gather(
        lease.acquire(name, "A", ttl_seconds=300, now=BASE),
        lease.acquire(name, "B", ttl_seconds=300, now=BASE),
    )
    assert sorted(r for r in results if r is not None) == [1]
    assert results.count(None) == 1
    current = await lease.current(name)
    assert current is not None and current[1] == 1


async def test_redrive_refused_when_active_key_recurred(session_factory) -> None:
    # A dead job whose logical key recurred (a fresh active row now holds it)
    # cannot be revived without breaking the active-idem index. redrive() must
    # catch that violation and return False (no 500), leaving both rows intact.
    epoch = await _acquire_coordinator(session_factory)
    queue = SABackgroundJobQueue(session_factory)
    dead_id = await queue.enqueue(
        _spec("recurring-key", epoch=epoch, max_attempts=1), now=BASE,
    )
    assert dead_id is not None
    await queue.claim("A", now=BASE, limit=10, lease_seconds=60)
    assert await queue.fail(dead_id, "A", error="boom", now=BASE)
    assert (await queue.get(dead_id)).status == JobStatus.DEAD

    fresh_id = await queue.enqueue(_spec("recurring-key", epoch=epoch), now=BASE)
    assert fresh_id is not None and fresh_id != dead_id

    assert await queue.redrive(dead_id) is False
    assert (await queue.get(dead_id)).status == JobStatus.DEAD
    assert (await queue.get(fresh_id)).status == JobStatus.QUEUED


async def test_journal_record_is_idempotent_across_restarts_and_concurrent_calls(
    session_factory,
) -> None:
    journal = SATickJournal(session_factory)
    entries = [
        (300, "character_tick", "char-1"),
        (300, "social_tick", None),
    ]
    await asyncio.gather(
        journal.record(entries, now=BASE),
        journal.record(entries, now=BASE + timedelta(seconds=1)),
        SATickJournal(session_factory).record(entries, now=BASE + timedelta(seconds=2)),
    )
    rows = await journal.list_range(from_bucket=300, to_bucket=300)
    assert [(bucket, kind, character_id) for bucket, kind, character_id, _ in rows] == [
        (300, "character_tick", "char-1"),
        (300, "social_tick", None),
    ]
    assert await journal.earliest_bucket_after(
        after_bucket=299, to_bucket=10**12,
    ) == 300
    assert await journal.list_bucket(300) == rows


async def test_cursor_advance_requires_live_fenced_leader_and_is_monotonic(
    session_factory,
) -> None:
    lease = SABackgroundCoordinatorLease(session_factory)
    cursor = SABackgroundCoordinatorCursor(session_factory)
    assert await lease.acquire("cursor-lease", "A", ttl_seconds=30, now=BASE) == 1
    await cursor.set("due-scan", {"last_bucket": 100, "other": "preserved"})

    assert await cursor.advance_if_leader(
        "due-scan", 200, "cursor-lease", "A", 1, BASE,
    )
    assert await cursor.advance_if_leader(
        "due-scan", 150, "cursor-lease", "A", 1, BASE,
    )
    assert await cursor.get("due-scan") == {
        "last_bucket": 200, "other": "preserved",
    }

    expired = BASE + timedelta(seconds=31)
    assert await lease.acquire("cursor-lease", "B", ttl_seconds=30, now=expired) == 2
    assert not await cursor.advance_if_leader(
        "due-scan", 300, "cursor-lease", "A", 1, expired,
    )
    assert not await cursor.advance_if_leader(
        "due-scan", 300, "cursor-lease", "B", 1, expired,
    )
    assert not await cursor.advance_if_leader(
        "due-scan", 300, "cursor-lease", "B", 2,
        expired + timedelta(seconds=31),
    )
    assert await cursor.get("due-scan") == {
        "last_bucket": 200, "other": "preserved",
    }


async def test_cursor_concurrent_stale_and_current_epochs_only_current_wins(
    session_factory,
) -> None:
    lease = SABackgroundCoordinatorLease(session_factory)
    cursor = SABackgroundCoordinatorCursor(session_factory)
    assert await lease.acquire("cursor-race", "A", ttl_seconds=1, now=BASE) == 1
    later = BASE + timedelta(seconds=5)
    assert await lease.acquire("cursor-race", "B", ttl_seconds=300, now=later) == 2

    results = await asyncio.gather(
        cursor.advance_if_leader("race", 10, "cursor-race", "A", 1, later),
        cursor.advance_if_leader("race", 20, "cursor-race", "B", 2, later),
    )
    assert results.count(True) == 1
    assert results == [False, True]
    assert await cursor.get("race") == {"last_bucket": 20}


async def test_cursor_stale_advance_waits_for_takeover_lock(session_factory) -> None:
    lease = SABackgroundCoordinatorLease(session_factory)
    cursor = SABackgroundCoordinatorCursor(session_factory)
    assert await lease.acquire("cursor-lock", "A", ttl_seconds=300, now=BASE) == 1

    async with session_factory() as taker:
        row = await taker.get(
            BackgroundRuntimeLeaseRow, "cursor-lock", with_for_update=True,
        )
        row.owner_id = "B"
        row.epoch = 2
        row.lease_until = BASE + timedelta(seconds=300)
        await taker.flush()
        advance = asyncio.create_task(cursor.advance_if_leader(
            "locked", 99, "cursor-lock", "A", 1, BASE,
        ))
        await asyncio.sleep(0.3)
        assert not advance.done()
        await taker.commit()

    assert await advance is False
    assert await cursor.get("locked") is None


async def test_journal_and_cursor_round_trip(session_factory) -> None:
    journal = SATickJournal(session_factory)
    cursor = SABackgroundCoordinatorCursor(session_factory)

    await journal.record(
        [
            (100, "character_tick", "char-1"),
            (100, "social_tick", None),
            (200, "character_tick", "char-2"),
        ],
        now=BASE,
    )
    rows = await journal.list_range(from_bucket=50, to_bucket=150)
    assert {(b, k, c) for b, k, c, _ in rows} == {
        (100, "character_tick", "char-1"),
        (100, "social_tick", None),
    }

    removed = await journal.prune(now=BASE + timedelta(days=8))
    assert removed == 3

    assert await cursor.get("due-scan") is None
    await cursor.set("due-scan", {"last_bucket": 200})
    assert await cursor.get("due-scan") == {"last_bucket": 200}
    await cursor.set("due-scan", {"last_bucket": 201})
    assert await cursor.get("due-scan") == {"last_bucket": 201}


async def test_full_row_round_trip_via_get(session_factory) -> None:
    epoch = await _acquire_coordinator(session_factory)
    queue = SABackgroundJobQueue(session_factory)
    job_id = await queue.enqueue(_spec(
        "round-trip",
        epoch=epoch,
        priority=2,
        tenant_id="tenant-1",  # explicit: this test asserts tenant round-trip
        payload={"character_id": "char-1", "date": "2026-07-20"},
    ), now=BASE)
    assert job_id is not None

    job = await queue.get(job_id)
    assert job is not None
    assert job.kind == "character_tick"
    assert job.status == JobStatus.QUEUED
    assert job.priority == 2
    assert job.fencing_epoch == epoch
    assert job.tenant_id == "tenant-1"
    assert job.character_id == "char-1"
    assert job.payload == {"character_id": "char-1", "date": "2026-07-20"}
    assert job.attempt_count == 0


async def test_tenant_fairness_caps_one_claimed_per_tenant(session_factory) -> None:
    # P3-C §5: the correlated NOT EXISTS + per-batch dedup keep at most one
    # claimed job per tenant cluster-wide. tenant_id=None jobs stay exempt.
    epoch = await _acquire_coordinator(session_factory)
    queue = SABackgroundJobQueue(session_factory)
    for tenant in ("tenant-a", "tenant-b"):
        for i in range(3):
            assert await queue.enqueue(
                _spec(
                    f"{tenant}-{i}", epoch=epoch, tenant_id=tenant,
                    character_id=f"{tenant}-{i}",
                ),
                now=BASE,
            ) is not None

    # A single claim batch keeps at most one job per tenant.
    claimed = await queue.claim("w1", now=BASE, limit=10, lease_seconds=60)
    assert sorted(c.tenant_id for c in claimed) == ["tenant-a", "tenant-b"]

    # While those are live-claimed, a second worker sees no more of either
    # tenant (correlated NOT EXISTS excludes tenants with a live claim).
    second = await queue.claim(
        "w2", now=BASE + timedelta(seconds=5), limit=10, lease_seconds=60,
    )
    assert second == []

    # None-tenant jobs remain exempt: both claimable in one batch.
    for i in range(2):
        assert await queue.enqueue(
            _spec(f"none-{i}", epoch=epoch, tenant_id=None, character_id=f"none-{i}"),
            now=BASE,
        ) is not None
    third = await queue.claim(
        "w3", now=BASE + timedelta(seconds=5), limit=10, lease_seconds=60,
    )
    assert sorted(c.character_id for c in third) == ["none-0", "none-1"]
