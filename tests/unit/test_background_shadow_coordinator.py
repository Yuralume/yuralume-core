"""Shadow coordinator behaviour (HOSTED_CORE_SCALING §3.2 / §13 Phase 2 / Phase 5).

Drives the coordinator against the in-memory queue/lease/cursor/journal twins
with a frozen clock. After the Phase 5 §13 cutover COMPLETES the distributed path
enqueues NOTHING per bucket — the last whole-table sweep (the global social_tick)
is gone; cross-character work is per-target chains reseeded by the reconcile, and
per-character character_tick was already replaced. Only the leader lease is
maintained and, when a reconciler is wired, the low-frequency reseed runs. The
embedded+shadow mirror path is unchanged. Also covers lease-loss → passive →
re-acquire with a bumped epoch and the hourly retention prune.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.background_shadow_coordinator import (
    ShadowCoordinator,
    bucket_for,
)
from kokoro_link.contracts.background_jobs import COORDINATOR_LEASE_NAME
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorCursor,
    InMemoryBackgroundCoordinatorLease,
    InMemoryBackgroundJobQueue,
    InMemoryTickJournal,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
BUCKET = 300


@dataclass
class _FrozenClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


@dataclass
class _FakeCharacter:
    id: str
    user_id: str = "default"
    frozen: bool = False
    subscription_locked: bool = False


class _FakeCharacterRepo:
    def __init__(self, characters: list[_FakeCharacter]) -> None:
        self._characters = characters

    async def list_active(self) -> list[_FakeCharacter]:
        return [c for c in self._characters if not c.frozen]

    async def get(self, character_id: str):
        return next(
            (c for c in self._characters if c.id == character_id), None,
        )

    async def list(self) -> list[_FakeCharacter]:
        return list(self._characters)


def _wiring(characters: list[_FakeCharacter]):
    lease = InMemoryBackgroundCoordinatorLease()
    queue = InMemoryBackgroundJobQueue(lease=lease)
    cursor = InMemoryBackgroundCoordinatorCursor(lease=lease)
    journal = InMemoryTickJournal()
    repo = _FakeCharacterRepo(characters)
    return lease, queue, cursor, journal, repo


def _coordinator(lease, queue, cursor, journal, repo, *, owner="coord-A"):
    return ShadowCoordinator(
        queue=queue,
        lease=lease,
        cursor=cursor,
        journal=journal,
        character_repository=repo,
        owner_id=owner,
        bucket_seconds=BUCKET,
        clock=_FrozenClock(BASE),
    )


def _mirror_coordinator(lease, queue, cursor, journal, repo, *, owner="coord-A"):
    return ShadowCoordinator(
        queue=queue,
        lease=lease,
        cursor=cursor,
        journal=journal,
        character_repository=repo,
        owner_id=owner,
        bucket_seconds=BUCKET,
        clock=_FrozenClock(BASE),
        mirror_from_journal=True,
    )


def _journal_entries(*rows: tuple[int, str, str | None]) -> list[tuple[int, str, str | None]]:
    return list(rows)


async def test_distributed_pass_enqueues_nothing_per_bucket() -> None:
    # Phase 5 §13 cutover COMPLETE: the distributed coordinator no longer enqueues
    # a global social_tick (nor a per-character character_tick) per bucket. The
    # cross-character work is per-target chains reseeded by the reconcile (wired
    # separately). A bare coordinator (no reconciler) enqueues nothing but still
    # becomes leader so a wired reconciler could reseed.
    chars = [_FakeCharacter("c1"), _FakeCharacter("c2")]
    lease, queue, cursor, journal, repo = _wiring(chars)
    coord = _coordinator(lease, queue, cursor, journal, repo)

    await coord.run_once(now=BASE)

    stats = await queue.stats(now=BASE)
    assert stats.total == 0  # nothing enqueued per bucket
    assert coord.epoch == 1  # but leadership is held for the reconcile
    snap = coord.snapshot()
    assert snap.enqueued == 0
    assert snap.deduped == 0


async def test_second_run_same_bucket_enqueues_nothing_new() -> None:
    chars = [_FakeCharacter("c1"), _FakeCharacter("c2")]
    lease, queue, cursor, journal, repo = _wiring(chars)
    coord = _coordinator(lease, queue, cursor, journal, repo)

    await coord.run_once(now=BASE)
    await coord.run_once(now=BASE)  # still nothing to enqueue

    assert (await queue.stats(now=BASE)).total == 0
    assert coord.snapshot().enqueued == 0


async def test_lease_lost_drops_to_passive_then_reacquires_new_epoch() -> None:
    # The leader-lease invariant survives the §13 cutover even though the pass
    # enqueues nothing: A leads (epoch 1); B steals after A's TTL; A's renew fails
    # → passive; B expires → A re-acquires with a bumped epoch.
    chars = [_FakeCharacter("c1")]
    lease, queue, cursor, journal, repo = _wiring(chars)
    coord = _coordinator(lease, queue, cursor, journal, repo, owner="A")

    await coord.run_once(now=BASE)
    assert coord.epoch == 1

    # Another coordinator B steals the lease after A's TTL expires.
    t1 = BASE + timedelta(seconds=400)
    assert await lease.acquire(
        COORDINATOR_LEASE_NAME, "B", ttl_seconds=30, now=t1,
    ) == 2

    # t2: A's renew fails → passive, and B still holds a live lease.
    t2 = BASE + timedelta(seconds=410)
    await coord.run_once(now=t2)
    assert coord.epoch is None
    assert coord.snapshot().lease_lost == 1

    # t3: B's lease has expired → A re-acquires, ownership change bumps epoch→3.
    t3 = BASE + timedelta(seconds=500)
    await coord.run_once(now=t3)
    assert coord.epoch == 3


class _FakeOperatorRepo:
    def __init__(self, tenants: dict[str, str | None]) -> None:
        self._tenants = tenants  # operator_id -> cloud_tenant_id

    async def get(self, operator_id: str):
        if operator_id not in self._tenants:
            return None
        return OperatorProfile(
            id=operator_id,
            display_name="Op",
            cloud_tenant_id=self._tenants[operator_id],
            auth_provider="cloud" if self._tenants[operator_id] else "local",
        )


async def test_mirror_still_stamps_operator_and_resolved_tenant() -> None:
    # H3(a) on the embedded+shadow mirror path (UNCHANGED by the §13 cutover):
    # each mirrored character_tick job carries the authoritative operator AND the
    # operator's resolved Cloud tenant, cached per batch. (In the distributed
    # path this stamping now lives in the DueJobReconciler, covered there.)
    chars = [
        _FakeCharacter("c1", user_id="op-cloud"),
        _FakeCharacter("c2", user_id="op-cloud"),
        _FakeCharacter("c3", user_id="op-local"),
    ]
    lease, queue, cursor, journal, repo = _wiring(chars)
    coord = ShadowCoordinator(
        queue=queue, lease=lease, cursor=cursor, journal=journal,
        character_repository=repo,
        operator_profile_repository=_FakeOperatorRepo({
            "op-cloud": "tenant-A", "op-local": None,
        }),
        owner_id="coord-A", bucket_seconds=BUCKET, clock=_FrozenClock(BASE),
        mirror_from_journal=True,
    )
    bucket = bucket_for(BASE, BUCKET)
    await journal.record(
        [
            (bucket, "character_tick", "c1"),
            (bucket, "character_tick", "c2"),
            (bucket, "character_tick", "c3"),
            (bucket, "social_tick", None),
        ],
        now=BASE,
    )

    await coord.run_once(now=BASE)

    records = {
        r.character_id: r for r in queue._records.values()  # noqa: SLF001
        if r.kind == "character_tick"
    }
    assert records["c1"].operator_id == "op-cloud"
    assert records["c1"].tenant_id == "tenant-A"
    assert records["c2"].tenant_id == "tenant-A"  # same operator, cached
    assert records["c3"].operator_id == "op-local"
    assert records["c3"].tenant_id is None  # local operator carries no tenant


async def test_mirror_without_social_signal_enqueues_nothing_and_keeps_cursor() -> None:
    chars = [_FakeCharacter("c1")]
    lease, queue, cursor, journal, repo = _wiring(chars)
    coord = _mirror_coordinator(lease, queue, cursor, journal, repo)

    await coord.run_once(now=BASE)

    assert (await queue.stats(now=BASE)).total == 0
    assert await cursor.get("shadow-enqueue") is None
    assert coord.snapshot().blocked_missing_signal == 1


async def test_mirror_delayed_signal_replays_earliest_complete_bucket() -> None:
    chars = [_FakeCharacter("c1")]
    lease, queue, cursor, journal, repo = _wiring(chars)
    coord = _mirror_coordinator(lease, queue, cursor, journal, repo)
    bucket = bucket_for(BASE, BUCKET)

    await coord.run_once(now=BASE)
    await journal.record(_journal_entries((bucket, "character_tick", "c1"), (bucket, "social_tick", None)), now=BASE)
    await coord.run_once(now=BASE + timedelta(seconds=BUCKET * 2))

    assert (await queue.stats(now=BASE)).kind_counts == {"character_tick": 1, "social_tick": 1}
    assert await cursor.get("shadow-enqueue") == {"last_bucket": bucket}


async def test_mirror_skips_wall_buckets_without_journal_and_uses_earliest_signal() -> None:
    chars = [_FakeCharacter("c1")]
    lease, queue, cursor, journal, repo = _wiring(chars)
    coord = _mirror_coordinator(lease, queue, cursor, journal, repo)
    first = bucket_for(BASE, BUCKET)
    later = first + 3
    await journal.record(
        _journal_entries((later, "character_tick", "c1"), (later, "social_tick", None)),
        now=BASE,
    )

    await coord.run_once(now=BASE + timedelta(seconds=BUCKET * 4))

    assert await cursor.get("shadow-enqueue") == {"last_bucket": later}
    assert {r.idempotency_key for r in queue._records.values()} == {
        f"character_tick:c1:{later}", f"social_tick:{later}",
    }


async def test_mirror_filters_journal_rows_deduplicates_and_skips_missing_character() -> None:
    chars = [_FakeCharacter("c1"), _FakeCharacter("c2")]
    lease, queue, cursor, journal, repo = _wiring(chars)
    coord = _mirror_coordinator(lease, queue, cursor, journal, repo)
    bucket = bucket_for(BASE, BUCKET)
    await journal.record(
        _journal_entries(
            (bucket, "character_tick", "c2"),
            (bucket, "character_tick", "c1"),
            (bucket, "character_tick", "c1"),
            (bucket, "character_tick", "missing"),
            (bucket, "social_tick", None),
            (bucket, "social_tick", None),
            (bucket, "wall_tick", "c1"),
        ),
        now=BASE,
    )

    await coord.run_once(now=BASE)

    assert (await queue.stats(now=BASE)).kind_counts == {"character_tick": 3, "social_tick": 1}
    assert coord.snapshot().journal_skipped_missing_character == 1
    assert await cursor.get("shadow-enqueue") == {"last_bucket": bucket}


async def test_mirror_partial_bucket_waits_for_social_commit_marker() -> None:
    chars = [_FakeCharacter("c1")]
    lease, queue, cursor, journal, repo = _wiring(chars)
    coord = _mirror_coordinator(lease, queue, cursor, journal, repo)
    bucket = bucket_for(BASE, BUCKET)
    await journal.record(
        _journal_entries((bucket, "character_tick", "c1")), now=BASE,
    )

    await coord.run_once(now=BASE)
    assert (await queue.stats(now=BASE)).total == 0
    assert await cursor.get("shadow-enqueue") is None
    assert coord.snapshot().blocked_missing_signal == 1

    await journal.record(_journal_entries((bucket, "social_tick", None)), now=BASE)
    await coord.run_once(now=BASE)
    assert (await queue.stats(now=BASE)).total == 2
    assert await cursor.get("shadow-enqueue") == {"last_bucket": bucket}


async def test_mirror_chooses_earliest_complete_bucket_in_order() -> None:
    chars = [_FakeCharacter("c1")]
    lease, queue, cursor, journal, repo = _wiring(chars)
    coord = _mirror_coordinator(lease, queue, cursor, journal, repo)
    first = bucket_for(BASE, BUCKET)
    second = first + 1
    await journal.record(
        _journal_entries(
            (second, "character_tick", "c1"), (second, "social_tick", None),
            (first, "character_tick", "c1"), (first, "social_tick", None),
        ),
        now=BASE,
    )

    await coord.run_once(now=BASE + timedelta(seconds=BUCKET * 2))

    assert await cursor.get("shadow-enqueue") == {"last_bucket": first}
    assert {r.idempotency_key for r in queue._records.values()} == {
        f"character_tick:c1:{first}", f"social_tick:{first}",
    }


async def test_mirror_restart_resumes_after_durable_cursor() -> None:
    chars = [_FakeCharacter("c1")]
    lease, queue, cursor, journal, repo = _wiring(chars)
    first = bucket_for(BASE, BUCKET)
    await journal.record(
        _journal_entries((first, "character_tick", "c1"), (first, "social_tick", None)),
        now=BASE,
    )
    coord = _mirror_coordinator(lease, queue, cursor, journal, repo)
    await coord.run_once(now=BASE)
    second = first + 1
    await journal.record(
        _journal_entries((second, "character_tick", "c1"), (second, "social_tick", None)),
        now=BASE,
    )
    restarted = _mirror_coordinator(lease, queue, cursor, journal, repo)

    await restarted.run_once(now=BASE + timedelta(seconds=BUCKET * 2))

    assert await cursor.get("shadow-enqueue") == {"last_bucket": second}
    assert (await queue.stats(now=BASE)).total == 4


async def test_mirror_fencing_loss_does_not_advance_cursor() -> None:
    chars = [_FakeCharacter("c1")]
    lease, queue, cursor, journal, repo = _wiring(chars)
    bucket = bucket_for(BASE, BUCKET)
    await journal.record(
        _journal_entries((bucket, "character_tick", "c1"), (bucket, "social_tick", None)),
        now=BASE,
    )
    assert await lease.acquire(COORDINATOR_LEASE_NAME, "A", ttl_seconds=30, now=BASE) == 1
    takeover = BASE + timedelta(seconds=60)
    assert await lease.acquire(COORDINATOR_LEASE_NAME, "B", ttl_seconds=300, now=takeover) == 2
    coord = _mirror_coordinator(lease, queue, cursor, journal, repo, owner="A")
    coord._epoch = 1  # noqa: SLF001

    await coord._enqueue_from_journal(1, BASE)  # noqa: SLF001

    assert await cursor.get("shadow-enqueue") is None
    assert coord.epoch is None
    assert coord.snapshot().lease_lost == 1


async def test_non_mirror_never_reads_journal_and_enqueues_nothing() -> None:
    chars = [_FakeCharacter("c1")]
    lease, queue, cursor, journal, repo = _wiring(chars)

    async def fail_if_read(*, from_bucket, to_bucket):
        raise AssertionError("non-mirror coordinator must not read journal ranges")

    journal.list_range = fail_if_read
    coord = _coordinator(lease, queue, cursor, journal, repo)

    await coord.run_once(now=BASE)

    # Distributed (non-mirror) coordinator reads no journal and enqueues nothing
    # per bucket (§13 cutover); reseeding is the reconciler's job.
    assert (await queue.stats(now=BASE)).total == 0


async def test_hourly_retention_prune_runs_on_cadence() -> None:
    chars = [_FakeCharacter("c1")]
    lease, queue, cursor, journal, repo = _wiring(chars)
    coord = _coordinator(lease, queue, cursor, journal, repo)

    queue_prune_calls: list[datetime] = []
    journal_prune_calls: list[datetime] = []
    _orig_q = queue.prune
    _orig_j = journal.prune

    async def _spy_q(*, now):
        queue_prune_calls.append(now)
        return await _orig_q(now=now)

    async def _spy_j(*, now):
        journal_prune_calls.append(now)
        return await _orig_j(now=now)

    queue.prune = _spy_q
    journal.prune = _spy_j

    await coord.run_once(now=BASE)  # first pass → prune runs
    await coord.run_once(now=BASE + timedelta(seconds=30))  # within 1h → skip
    await coord.run_once(now=BASE + timedelta(seconds=4000))  # >1h → prune

    assert len(queue_prune_calls) == 2
    assert len(journal_prune_calls) == 2
