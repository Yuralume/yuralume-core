"""Shadow dry-run worker behaviour (HOSTED_CORE_SCALING §3.3 / §13 Phase 2).

Claims coordinator-enqueued jobs against the in-memory queue and validates them
read-only: healthy → would_run true; frozen / missing / subscription-locked →
skip with a reason; validation crash → fail with backoff → dead at the attempt
cap. Never runs a provider or mutates a character (validation reads only).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.background_shadow_worker import (
    ShadowDryRunWorker,
)
from kokoro_link.application.services.execution_mode_runner import (
    ExecutionDisposition,
)
from kokoro_link.contracts.execution_mode import MODE_DISTRIBUTED
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)
from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    BackgroundJobSpec,
    JobStatus,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
    InMemoryBackgroundJobQueue,
)
from kokoro_link.infrastructure.repositories.in_memory_cloud_subscription import (
    InMemoryCloudSubscriptionRepository,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class _FakeCharacter:
    id: str
    user_id: str = "default"
    frozen: bool = False
    subscription_locked: bool = False


class _FakeCharacterRepo:
    def __init__(self, characters: list[_FakeCharacter]) -> None:
        self._characters = characters

    async def get(self, character_id: str):
        return next(
            (c for c in self._characters if c.id == character_id), None,
        )

    async def list_active(self):
        return [c for c in self._characters if not c.frozen]

    async def list(self):
        return list(self._characters)


class _ExplodingCharacterRepo:
    async def get(self, character_id: str):
        raise RuntimeError("db down")

    async def list_active(self):
        return []

    async def list(self):
        return []


async def _queue_with(job_specs, *, epoch_now=BASE):
    lease = InMemoryBackgroundCoordinatorLease()
    epoch = await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=300, now=epoch_now,
    )
    queue = InMemoryBackgroundJobQueue(lease=lease)
    ids = []
    for spec in job_specs(epoch):
        ids.append(await queue.enqueue(spec, now=BASE))
    return queue, ids


def _char_spec(character_id: str, *, epoch: int, kind: str = "character_tick"):
    return BackgroundJobSpec(
        kind=kind,
        idempotency_key=f"{kind}:{character_id}:0",
        due_at=BASE,
        fencing_epoch=epoch,
        character_id=character_id if kind == "character_tick" else None,
        payload={"character_id": character_id, "bucket": 0},
    )


def _worker(queue, repo, *, guard=None):
    return ShadowDryRunWorker(
        queue=queue,
        character_repository=repo,
        worker_id="w1",
        subscription_access_guard=guard,
        dry_run_hold_seconds=0.0,
    )


async def test_healthy_character_completes_would_run_true() -> None:
    queue, ids = await _queue_with(lambda e: [_char_spec("c1", epoch=e)])
    worker = _worker(queue, _FakeCharacterRepo([_FakeCharacter("c1")]))

    claimed = await worker.run_once(now=BASE)

    assert claimed == 1
    job = await queue.get(ids[0])
    assert job is not None and job.status == JobStatus.DONE
    assert job.outcome == {
        "dry_run": True, "would_run": True, "skip_reason": None,
    }
    snap = worker.snapshot()
    assert snap.claimed == 1 and snap.completed == 1 and snap.skipped == 0


async def test_social_tick_validates_trivially() -> None:
    queue, ids = await _queue_with(
        lambda e: [_char_spec("", epoch=e, kind="social_tick")],
    )
    worker = _worker(queue, _FakeCharacterRepo([]))

    await worker.run_once(now=BASE)

    job = await queue.get(ids[0])
    assert job is not None and job.status == JobStatus.DONE
    assert job.outcome["would_run"] is True
    assert worker.snapshot().completed == 1


async def test_frozen_character_skips_with_reason() -> None:
    queue, ids = await _queue_with(lambda e: [_char_spec("c1", epoch=e)])
    worker = _worker(
        queue, _FakeCharacterRepo([_FakeCharacter("c1", frozen=True)]),
    )

    await worker.run_once(now=BASE)

    job = await queue.get(ids[0])
    assert job is not None and job.status == JobStatus.DONE
    assert job.outcome == {
        "dry_run": True, "would_run": False, "skip_reason": "frozen",
    }
    snap = worker.snapshot()
    assert snap.skipped == 1 and snap.completed == 0


async def test_missing_character_skips_with_reason() -> None:
    queue, ids = await _queue_with(lambda e: [_char_spec("ghost", epoch=e)])
    worker = _worker(queue, _FakeCharacterRepo([]))

    await worker.run_once(now=BASE)

    job = await queue.get(ids[0])
    assert job is not None and job.outcome["skip_reason"] == "missing_character"
    assert job.outcome["would_run"] is False
    assert worker.snapshot().skipped == 1


async def test_subscription_locked_skips_via_real_guard() -> None:
    # Real SubscriptionAccessGuard + in-memory subscription repo: a cloud
    # operator whose tenant is locked makes the character disallowed.
    cloud_op = OperatorProfile(
        id="op-cloud",
        display_name="Cloud Op",
        cloud_tenant_id="t1",
        auth_provider="cloud",
    )

    class _FakeOps:
        async def get(self, operator_id: str):
            return cloud_op if operator_id == "op-cloud" else None

    subs = InMemoryCloudSubscriptionRepository()
    await subs.set_locked("t1", locked=True)
    guard = SubscriptionAccessGuard(
        subscription_repository=subs,
        operator_profile_repository=_FakeOps(),
    )

    queue, ids = await _queue_with(lambda e: [_char_spec("c1", epoch=e)])
    repo = _FakeCharacterRepo([_FakeCharacter("c1", user_id="op-cloud")])
    worker = _worker(queue, repo, guard=guard)

    await worker.run_once(now=BASE)

    job = await queue.get(ids[0])
    assert job is not None
    assert job.outcome["skip_reason"] == "subscription_locked"
    assert job.outcome["would_run"] is False


async def test_validation_exception_fails_with_backoff_then_dead_at_cap() -> None:
    queue, ids = await _queue_with(
        lambda e: [
            BackgroundJobSpec(
                kind="character_tick",
                idempotency_key="character_tick:c1:0",
                due_at=BASE,
                fencing_epoch=e,
                character_id="c1",
                max_attempts=2,
                payload={"character_id": "c1", "bucket": 0},
            ),
        ],
    )
    worker = _worker(queue, _ExplodingCharacterRepo())
    job_id = ids[0]

    # Attempt 1: claim → validate crashes → fail with backoff (requeued).
    await worker.run_once(now=BASE)
    job = await queue.get(job_id)
    assert job is not None
    assert job.status == JobStatus.QUEUED
    assert job.attempt_count == 1
    assert job.last_error and "RuntimeError" in job.last_error
    # Backoff = min(5 * 2**1, 300) = 10s → not yet due at BASE.
    assert job.due_at == BASE + timedelta(seconds=10)

    # Attempt 2: reclaim after backoff → validate crashes → attempt hits the
    # cap (2) → the queue retires it to dead.
    later = BASE + timedelta(seconds=15)
    await worker.run_once(now=later)
    job = await queue.get(job_id)
    assert job is not None and job.status == JobStatus.DEAD
    assert worker.snapshot().failed == 2


class _FakeOperatorRepo:
    def __init__(self, operators: dict[str, str | None]) -> None:
        # operator_id -> cloud_tenant_id
        self._operators = operators

    async def get(self, operator_id: str):
        if operator_id not in self._operators:
            return None
        return OperatorProfile(
            id=operator_id,
            display_name="Op",
            cloud_tenant_id=self._operators[operator_id],
            auth_provider="cloud" if self._operators[operator_id] else "local",
        )


def _identity_spec(
    *, epoch: int, character_id="c1", operator_id="op1",
    tenant_id=None, payload_character_id=None,
):
    return BackgroundJobSpec(
        kind="character_tick",
        idempotency_key=f"character_tick:{character_id}:0",
        due_at=BASE,
        fencing_epoch=epoch,
        operator_id=operator_id,
        tenant_id=tenant_id,
        character_id=character_id,
        payload={
            "character_id": payload_character_id or character_id, "bucket": 0,
        },
    )


async def test_missing_character_precedes_identity_mismatch() -> None:
    queue, ids = await _queue_with(
        lambda e: [_identity_spec(epoch=e, character_id="ghost", operator_id="old")],
    )
    worker = _worker(queue, _FakeCharacterRepo([]))

    await worker.run_once(now=BASE)

    job = await queue.get(ids[0])
    assert job is not None
    assert job.outcome["skip_reason"] == "missing_character"


    # The reloaded character's owner (user_id) no longer matches the operator the
    # job was enqueued for → fail-closed identity_mismatch (would_run=false).
    queue, ids = await _queue_with(
        lambda e: [_identity_spec(epoch=e, operator_id="op-OLD")],
    )
    repo = _FakeCharacterRepo([_FakeCharacter("c1", user_id="op-NEW")])
    worker = _worker(queue, repo)

    await worker.run_once(now=BASE)

    job = await queue.get(ids[0])
    assert job is not None
    assert job.outcome["skip_reason"] == "identity_mismatch"
    assert job.outcome["would_run"] is False
    assert worker.snapshot().skipped == 1


async def test_identity_mismatch_payload_character_skips() -> None:
    # The payload character id does not match the row id (a mis-routed payload).
    queue, ids = await _queue_with(
        lambda e: [_identity_spec(
            epoch=e, character_id="c1", operator_id="default",
            payload_character_id="SOMEONE-ELSE",
        )],
    )
    repo = _FakeCharacterRepo([_FakeCharacter("c1", user_id="default")])
    worker = _worker(queue, repo)

    await worker.run_once(now=BASE)

    job = await queue.get(ids[0])
    assert job is not None
    assert job.outcome["skip_reason"] == "identity_mismatch"


async def test_identity_mismatch_tenant_skips() -> None:
    # The job carries tenant t1, but the operator's CURRENT cloud tenant is t2
    # (the character migrated tenants under the job) → identity_mismatch.
    queue, ids = await _queue_with(
        lambda e: [_identity_spec(
            epoch=e, operator_id="op1", tenant_id="t1",
        )],
    )
    repo = _FakeCharacterRepo([_FakeCharacter("c1", user_id="op1")])
    worker = ShadowDryRunWorker(
        queue=queue,
        character_repository=repo,
        worker_id="w1",
        operator_profile_repository=_FakeOperatorRepo({"op1": "t2"}),
        dry_run_hold_seconds=0.0,
    )

    await worker.run_once(now=BASE)

    job = await queue.get(ids[0])
    assert job is not None
    assert job.outcome["skip_reason"] == "identity_mismatch"


async def test_matching_tenant_passes_identity() -> None:
    # Same tenant on both sides → identity OK → healthy would_run=true.
    queue, ids = await _queue_with(
        lambda e: [_identity_spec(
            epoch=e, operator_id="op1", tenant_id="t1",
        )],
    )
    repo = _FakeCharacterRepo([_FakeCharacter("c1", user_id="op1")])
    worker = ShadowDryRunWorker(
        queue=queue,
        character_repository=repo,
        worker_id="w1",
        operator_profile_repository=_FakeOperatorRepo({"op1": "t1"}),
        dry_run_hold_seconds=0.0,
    )

    await worker.run_once(now=BASE)

    job = await queue.get(ids[0])
    assert job is not None
    assert job.outcome["would_run"] is True
    assert job.outcome["skip_reason"] is None


# --------------------------------------------------------------------------- #
# §13 execution concurrency (finding #19): the execution batch runs jobs with
# bounded per-replica concurrency instead of one-in-flight sequential draining.
# --------------------------------------------------------------------------- #

class _FakeOwnership:
    async def get(self):
        return (MODE_DISTRIBUTED, 1)


class _ConcurrencyProbeRunner:
    """Records the peak number of ``execute`` calls in flight at once."""

    def __init__(self, *, target: int) -> None:
        self._target = target
        self.in_flight = 0
        self.max_in_flight = 0

    async def execute(self, job, *, would_run, skip_reason, now, worker_id, lease_seconds):  # noqa: ANN001,E501
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        # Cooperatively wait until the semaphore-bounded cohort has filled (or a
        # bounded number of yields) so simultaneous in-flight work is observable.
        for _ in range(200):
            if self.in_flight >= self._target:
                break
            await asyncio.sleep(0)
        await asyncio.sleep(0)
        self.in_flight -= 1
        return ExecutionDisposition("complete", outcome={"executed": True})


def _social_spec(index: int, *, epoch: int):
    return BackgroundJobSpec(
        kind="social_tick",
        idempotency_key=f"social_tick:{index}",
        due_at=BASE,
        fencing_epoch=epoch,
        character_id=None,
        payload={"bucket": 0},
    )


def _execution_worker(queue, probe, *, concurrency: int) -> ShadowDryRunWorker:
    return ShadowDryRunWorker(
        queue=queue,
        character_repository=_FakeCharacterRepo([]),
        worker_id="w1",
        dry_run_hold_seconds=0.0,
        runtime_ownership=_FakeOwnership(),
        execution_runner=probe,
        execution_concurrency=concurrency,
        claim_limit=8,
    )


async def test_execution_batch_runs_two_in_flight() -> None:
    queue, _ = await _queue_with(
        lambda e: [_social_spec(i, epoch=e) for i in range(4)],
    )
    probe = _ConcurrencyProbeRunner(target=2)
    worker = _execution_worker(queue, probe, concurrency=2)

    claimed = await worker.run_once(now=BASE)

    assert claimed == 4
    # Two jobs are processed simultaneously (spec concurrency), never more.
    assert probe.max_in_flight == 2
    assert worker.snapshot().completed == 4


async def test_execution_concurrency_knob_of_one_is_sequential() -> None:
    # With the knob at 1 the batch drains one-in-flight (the pre-fix behaviour),
    # proving the semaphore actually bounds concurrency.
    queue, _ = await _queue_with(
        lambda e: [_social_spec(i, epoch=e) for i in range(4)],
    )
    probe = _ConcurrencyProbeRunner(target=2)
    worker = _execution_worker(queue, probe, concurrency=1)

    await worker.run_once(now=BASE)

    assert probe.max_in_flight == 1


async def test_counters_track_a_mixed_batch() -> None:
    queue, _ = await _queue_with(
        lambda e: [
            _char_spec("c1", epoch=e),           # healthy → completed
            _char_spec("c2", epoch=e),           # frozen → skipped
            _char_spec("ghost", epoch=e),        # missing → skipped
            _char_spec("", epoch=e, kind="social_tick"),  # social → completed
        ],
    )
    repo = _FakeCharacterRepo([
        _FakeCharacter("c1"),
        _FakeCharacter("c2", frozen=True),
    ])
    worker = _worker(queue, repo)

    claimed = await worker.run_once(now=BASE)

    assert claimed == 4
    snap = worker.snapshot()
    assert snap.claimed == 4
    assert snap.completed == 2   # c1 + social
    assert snap.skipped == 2     # c2 frozen + ghost missing
    assert snap.failed == 0
    assert snap.lease_lost == 0
