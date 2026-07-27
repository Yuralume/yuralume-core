"""End-to-end shadow round trip against PostgreSQL (HOSTED_CORE_SCALING §13 P2 / Phase 5).

Seeds active + frozen characters via the SA repos and runs the coordinator (with
its Phase 5 reconciler wired) + worker passes over the REAL SA
queue/lease/cursor/journal adapters. After the §13 cutover the distributed work
set is: one global ``social_tick`` per bucket plus one job PER per-kind chain PER
active character (reseeded by the reconciler; frozen excluded by ``list_active``),
all dry-run-completed ``would_run=true``. The embedded+shadow mirror path
(``mirror_from_journal``) still mirrors ``character_tick`` unchanged.
Requires Docker (testcontainers); skipped automatically when unavailable.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from kokoro_link.application.services.background_shadow_coordinator import (
    ShadowCoordinator,
    bucket_for,
    bucket_start,
)
from kokoro_link.application.services.background_shadow_worker import (
    ShadowDryRunWorker,
)
from kokoro_link.application.services.account_runtime_profile import (
    PermissiveAccountRuntimeProfileResolver,
)
from kokoro_link.application.services.due_job_reconciler import DueJobReconciler
from kokoro_link.application.services.social_due_job_reconciler import (
    SocialDueJobReconciler,
)
from kokoro_link.application.services.due_job_scheduler import NextDueCalculator
from kokoro_link.contracts.due_jobs import (
    character_chain_kinds,
    social_character_chain_kinds,
)
from kokoro_link.infrastructure.persistence.sa_character_relationship_repository import (
    SACharacterRelationshipRepository,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.persistence.models import BackgroundJobRow
from kokoro_link.infrastructure.persistence.sa_background_jobs import (
    SABackgroundJobQueue,
)
from kokoro_link.infrastructure.persistence.sa_background_runtime import (
    SABackgroundCoordinatorCursor,
    SABackgroundCoordinatorLease,
    SATickJournal,
)
from kokoro_link.infrastructure.persistence.sa_character_repository import (
    SACharacterRepository,
)
from kokoro_link.infrastructure.persistence.sa_operator_profile_repository import (
    SAOperatorProfileRepository,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
BUCKET = 300


@dataclass(slots=True)
class _FrozenClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


async def _done_count(session_factory) -> int:
    async with session_factory() as session:
        return int((await session.execute(
            select(func.count()).select_from(BackgroundJobRow)
            .where(BackgroundJobRow.status == "done"),
        )).scalar_one())


def _new_character(name: str) -> Character:
    return Character.create(
        name=name, summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )


def _wire_reconciler(coordinator, queue, characters, *, session_factory=None) -> None:
    """Attach a Phase 5 reconciler so the coordinator seeds per-kind chains.

    ``reseed_jitter_seconds=0`` keeps the reseeded jobs now-due for deterministic
    claiming under the frozen clock; ``runtime_ownership=None`` lets it run
    whenever the coordinator is leader (no separate ownership row in this test).
    The §13 social reconciler is wired too (per character dream/peer + per pair
    encounter) so the distributed pass — which no longer enqueues ``social_tick``
    — relinks the cross-character chains."""
    calc = NextDueCalculator(
        resolver=PermissiveAccountRuntimeProfileResolver(),
    )
    social = None
    if session_factory is not None:
        social = SocialDueJobReconciler(
            queue=queue,
            character_repository=characters,
            relationship_repository=SACharacterRelationshipRepository(session_factory),
            next_due_calculator=calc,
            epoch_provider=lambda: coordinator.epoch,
            runtime_ownership=None,
            reseed_jitter_seconds=0,
        )
    reconciler = DueJobReconciler(
        queue=queue,
        character_repository=characters,
        next_due_calculator=calc,
        epoch_provider=lambda: coordinator.epoch,
        runtime_ownership=None,
        reseed_jitter_seconds=0,
        social_reconciler=social,
    )
    coordinator.set_due_job_reconciler(reconciler)


async def test_shadow_round_trip_active_only_enqueue_and_dry_run(
    session_factory,
) -> None:
    characters = SACharacterRepository(session_factory)
    active_a = _new_character("Active A")
    active_b = _new_character("Active B")
    frozen = _new_character("Frozen C")
    await characters.save(active_a)
    await characters.save(active_b)
    await characters.save(frozen)
    await characters.set_frozen(frozen.id, frozen=True, now=BASE, reason="manual")

    queue = SABackgroundJobQueue(session_factory)
    lease = SABackgroundCoordinatorLease(session_factory)
    cursor = SABackgroundCoordinatorCursor(session_factory)
    journal = SATickJournal(session_factory)

    coordinator = ShadowCoordinator(
        queue=queue, lease=lease, cursor=cursor, journal=journal,
        character_repository=characters, owner_id="coord-it",
        bucket_seconds=BUCKET, clock=_FrozenClock(BASE),
    )
    _wire_reconciler(coordinator, queue, characters, session_factory=session_factory)
    worker = ShadowDryRunWorker(
        queue=queue, character_repository=characters,
        worker_id="worker-it", dry_run_hold_seconds=0.0,
    )

    # §13 cutover COMPLETE: one coordinator pass enqueues NO social_tick — the
    # reconciler seeds every per-kind chain (character + social dream/peer) for
    # each ACTIVE character (frozen excluded by list_active); no relationships →
    # no encounter pair chains here.
    await coordinator.run_once(now=BASE)

    bucket = bucket_for(BASE, BUCKET)
    all_char_kinds = tuple(character_chain_kinds()) + tuple(social_character_chain_kinds())
    expected = len(all_char_kinds) * 2  # 2 active chars × (char + social) kinds
    async with session_factory() as session:
        kind_counts = dict((await session.execute(
            select(BackgroundJobRow.kind, func.count())
            .group_by(BackgroundJobRow.kind),
        )).all())
        char_ids = set((await session.execute(
            select(BackgroundJobRow.character_id)
            .where(BackgroundJobRow.character_id.is_not(None)),
        )).scalars().all())
    assert "social_tick" not in kind_counts  # cutover: no per-bucket social_tick
    assert not any(k == "character_tick" for k in kind_counts)  # cutover
    for kind in all_char_kinds:
        assert kind_counts[kind] == 2  # one per active character
    assert char_ids == {active_a.id, active_b.id}  # frozen never seeded

    # Worker drains: claim + dry-run-complete every job. Loop until empty.
    total_claimed = 0
    for _ in range(20):
        claimed = await worker.run_once(now=BASE)
        total_claimed += claimed
        if claimed == 0:
            break
    assert total_claimed == expected

    async with session_factory() as session:
        done = int((await session.execute(
            select(func.count()).select_from(BackgroundJobRow)
            .where(BackgroundJobRow.status == "done"),
        )).scalar_one())
        outcome_rows = (await session.execute(
            select(BackgroundJobRow.outcome_json),
        )).scalars().all()
    assert done == expected
    # Every dry-run outcome is would_run=true (all seeded actives are healthy;
    # the frozen one was never enqueued, so no skip appears here).
    import json

    for raw in outcome_rows:
        payload = json.loads(raw)
        assert payload["dry_run"] is True
        assert payload["would_run"] is True
    wsnap = worker.snapshot()
    assert wsnap.claimed == expected and wsnap.completed == expected
    assert wsnap.skipped == 0
    async with session_factory() as session:
        job_keys = (await session.execute(
            select(BackgroundJobRow.idempotency_key),
        )).scalars().all()
    # No social_tick anymore; the social chains are scoped per character.
    assert not any(k.startswith("social_tick:") for k in job_keys)
    assert any(k.startswith("persona_dream:") for k in job_keys)


async def test_shadow_round_trip_mirrors_complete_journal_bucket_into_queue(
    session_factory,
) -> None:
    characters = SACharacterRepository(session_factory)
    active = _new_character("Journal Active")
    missing_id = "missing-from-authoritative-reload"
    await characters.save(active)

    queue = SABackgroundJobQueue(session_factory)
    lease = SABackgroundCoordinatorLease(session_factory)
    cursor = SABackgroundCoordinatorCursor(session_factory)
    journal = SATickJournal(session_factory)
    bucket = bucket_for(BASE, BUCKET)
    await journal.record(
        [
            (bucket, "character_tick", active.id),
            (bucket, "character_tick", active.id),
            (bucket, "character_tick", missing_id),
            (bucket, "social_tick", None),
        ],
        now=BASE,
    )

    coordinator = ShadowCoordinator(
        queue=queue,
        lease=lease,
        cursor=cursor,
        journal=journal,
        character_repository=characters,
        owner_id="coord-journal-it",
        bucket_seconds=BUCKET,
        clock=_FrozenClock(bucket_start(108, BUCKET)),
        mirror_from_journal=True,
    )

    await coordinator.run_once(now=BASE)

    async with session_factory() as session:
        rows = (await session.execute(
            select(BackgroundJobRow.kind, BackgroundJobRow.character_id),
        )).all()
    assert set(rows) == {
        ("character_tick", active.id),
        ("character_tick", missing_id),
        ("social_tick", None),
    }
    assert await cursor.get("shadow-enqueue") == {"last_bucket": bucket}
    snap = coordinator.snapshot()
    assert snap.enqueued == 3
    assert snap.journal_skipped_missing_character == 1


async def test_concurrent_coordinator_and_two_workers_drain_disjointly(
    session_factory,
) -> None:
    # The real production topology: coordinator + multiple dry-run workers as
    # concurrent asyncio tasks over ONE shared session_factory (not sequential
    # run_once). Proves the three services co-run without duplicating or losing
    # work under a realistic dry-run hold that widens the claim→complete lease
    # window. A frozen clock pins the bucket so exactly one enqueue batch lands.
    characters = SACharacterRepository(session_factory)
    seeded = [_new_character(f"Active {i}") for i in range(6)]
    for character in seeded:
        await characters.save(character)
    # 6 active chars × per-kind chains (character + social dream/peer); §13 cutover
    # removed the global social_tick, and there are no relationships → no pairs.
    expected = len(seeded) * (
        len(character_chain_kinds()) + len(social_character_chain_kinds())
    )

    queue = SABackgroundJobQueue(session_factory)
    lease = SABackgroundCoordinatorLease(session_factory)
    cursor = SABackgroundCoordinatorCursor(session_factory)
    journal = SATickJournal(session_factory)
    clock = _FrozenClock(BASE)

    coordinator = ShadowCoordinator(
        queue=queue, lease=lease, cursor=cursor, journal=journal,
        character_repository=characters, owner_id="coord-cc",
        bucket_seconds=BUCKET, loop_seconds=0.1, clock=clock,
    )
    _wire_reconciler(coordinator, queue, characters, session_factory=session_factory)
    workers = [
        ShadowDryRunWorker(
            queue=queue, character_repository=characters,
            worker_id=f"worker-cc-{n}", loop_seconds=0.05,
            claim_limit=3, lease_seconds=60, dry_run_hold_seconds=0.02,
            clock=clock,
        )
        for n in range(2)
    ]

    await coordinator.start()
    for worker in workers:
        await worker.start()
    try:
        for _ in range(200):  # bounded ~10s
            if await _done_count(session_factory) >= expected:
                break
            await asyncio.sleep(0.05)
    finally:
        for worker in workers:
            await worker.stop()
        await coordinator.stop()

    # Every enqueued job completed exactly once — no loss, no double-complete.
    async with session_factory() as session:
        done_keys = (await session.execute(
            select(BackgroundJobRow.idempotency_key)
            .where(BackgroundJobRow.status == "done"),
        )).scalars().all()
        status_counts = dict((await session.execute(
            select(BackgroundJobRow.status, func.count())
            .group_by(BackgroundJobRow.status),
        )).all())
    assert len(done_keys) == expected
    assert len(set(done_keys)) == expected      # distinct → no duplicate done
    assert status_counts.get("dead", 0) == 0
    assert status_counts.get("failed", 0) == 0

    # Claims are disjoint across the two concurrent workers (each job charged
    # exactly one attempt) and no lease was lost (60s lease, frozen clock → no
    # expiry/reclaim churn).
    total_claimed = sum(w.snapshot().claimed for w in workers)
    assert total_claimed == expected
    assert all(w.snapshot().lease_lost == 0 for w in workers)


async def test_shadow_mirrors_sparse_journal_batches_in_order_and_compares_cleanly(
    session_factory, postgres_container,
) -> None:
    """Mirror three non-adjacent committed ticks through the real PG adapters.

    The journal cursor must consume the earliest complete batch on each pass;
    wall-clock gaps are intentionally not synthetic queue work.  The two
    characters are created through the same SA operator/character harness used
    by the soak integration test, so the worker reloads real persisted identity
    profiles before completing each dry-run job.
    """
    from scripts.soak_shadow_compare import compare_shadow, load_soak_rows

    operator_repository = SAOperatorProfileRepository(session_factory)
    characters = SACharacterRepository(session_factory)
    character_service = CharacterService(characters)
    operators = [
        OperatorProfile(
            id=f"sparse-mirror-op-{suffix}",
            display_name=f"Sparse mirror {suffix}",
            email=f"sparse-mirror-{suffix}@example.com",
            password_hash="x",
            is_admin=True,
        )
        for suffix in ("a", "b")
    ]
    for operator in operators:
        await operator_repository.save(operator)

    seeded = []
    for operator in operators:
        response = await character_service.create_character(
            CreateCharacterRequest(name=f"Sparse character {operator.id}"),
            user_id=operator.id,
        )
        seeded.append(response.id)
    character_ids = tuple(sorted(seeded))
    assert len(character_ids) == 2
    assert {c.user_id for c in await characters.list_active()} >= {
        operator.id for operator in operators
    }

    queue = SABackgroundJobQueue(session_factory)
    lease = SABackgroundCoordinatorLease(session_factory)
    cursor = SABackgroundCoordinatorCursor(session_factory)
    journal = SATickJournal(session_factory)
    coordinator = ShadowCoordinator(
        queue=queue,
        lease=lease,
        cursor=cursor,
        journal=journal,
        character_repository=characters,
        operator_profile_repository=operator_repository,
        owner_id="coord-sparse-journal-it",
        bucket_seconds=BUCKET,
        clock=_FrozenClock(bucket_start(108, BUCKET)),
        mirror_from_journal=True,
    )
    worker = ShadowDryRunWorker(
        queue=queue,
        character_repository=characters,
        operator_profile_repository=operator_repository,
        worker_id="worker-sparse-journal-it",
        dry_run_hold_seconds=0.0,
    )

    buckets = (100, 103, 107)
    for bucket in buckets:
        await journal.record(
            [
                *( (bucket, "character_tick", character_id)
                   for character_id in character_ids ),
                (bucket, "social_tick", None),
            ],
            now=bucket_start(bucket, BUCKET),
        )

    mirror_now = bucket_start(108, BUCKET)
    for expected_bucket in buckets:
        await coordinator.run_once(now=mirror_now)
        assert await cursor.get("shadow-enqueue") == {
            "last_bucket": expected_bucket,
        }

    # A fourth pass sees only the cursor/current-bucket gap and adds nothing.
    await coordinator.run_once(now=mirror_now)
    snap = coordinator.snapshot()
    assert snap.enqueued == 9
    assert snap.deduped == 0

    async with session_factory() as session:
        queued = (await session.execute(
            select(BackgroundJobRow.kind, BackgroundJobRow.character_id,
                   BackgroundJobRow.idempotency_key),
        )).all()
    assert sorted(
        (kind, character_id, int(key.rsplit(":", 1)[-1]))
        for kind, character_id, key in queued
    ) == sorted(
        (kind, character_id, bucket)
        for bucket in buckets
        for kind, character_id in (
            [("character_tick", character_ids[0]),
             ("character_tick", character_ids[1]),
             ("social_tick", None)]
        )
    )
    assert {bucket for _kind, _character_id, bucket in (
        (kind, character_id, int(key.rsplit(":", 1)[-1]))
        for kind, character_id, key in queued
    )} == set(buckets)

    for _ in range(5):
        if await worker.run_once(now=mirror_now) == 0:
            break

    async with session_factory() as session:
        rows = (await session.execute(
            select(BackgroundJobRow.kind, BackgroundJobRow.character_id,
                   BackgroundJobRow.status),
        )).all()
    assert len(rows) == 9
    assert all(status == "done" for _kind, _character_id, status in rows)

    url = postgres_container.get_connection_url()
    if "+psycopg2" in url:
        url = url.replace("+psycopg2", "+asyncpg")
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    from_ts = bucket_start(99, BUCKET)
    to_ts = bucket_start(108, BUCKET)
    journal_entries, job_rows, window = await load_soak_rows(
        database_url=url,
        from_ts=from_ts,
        to_ts=to_ts,
        bucket_seconds=BUCKET,
    )
    report = compare_shadow(
        journal_entries, job_rows, bucket_seconds=BUCKET, window=window,
    )
    assert report.missed_count == 0
    assert report.duplicate_count == 0
    assert report.wrong_run_count == 0
    assert report.identity_mismatch_count == 0
    assert report.invalid_outcome_count == 0
    assert report.passes(min_buckets=len(buckets))
