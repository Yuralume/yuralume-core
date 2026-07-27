"""End-to-end soak comparison against PostgreSQL (HOSTED_CORE_SCALING §13 P2).

Seeds a small population through the soak seeder's importable ``seed_population``
(2 operators × 3 characters, 1 frozen), enqueues + dry-run-completes shadow jobs
across two buckets via the REAL P2-A/P2-B adapters, records the matching embedded
tick journal, then runs the ``soak_shadow_compare`` loader + pure comparison
end-to-end:

* the healthy population yields an all-zero report (missed = duplicates =
  wrong_runs = 0);
* deleting one completed job makes the judge report exactly one 漏job (missed).

Requires Docker (testcontainers); skipped automatically when unavailable.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import delete, select

from kokoro_link.application.services.background_shadow_coordinator import (
    ShadowCoordinator,
    bucket_for,
)
from kokoro_link.application.services.background_shadow_worker import (
    ShadowDryRunWorker,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.domain.entities.operator_profile import OperatorProfile
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
from scripts.soak_seed_population import build_seed_plan, seed_population
from scripts.soak_shadow_compare import compare_shadow, load_soak_rows

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
BUCKET = 300


class _FrozenClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def _async_url(container) -> str:
    url = container.get_connection_url()
    if "+psycopg2" in url:
        return url.replace("+psycopg2", "+asyncpg")
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+asyncpg://", 1)
    return url


async def _drain(worker: ShadowDryRunWorker, now: datetime) -> None:
    for _ in range(10):
        if await worker.run_once(now=now) == 0:
            break


async def test_soak_compare_all_zero_then_detects_one_missed(
    session_factory, postgres_container,
) -> None:
    character_repository = SACharacterRepository(session_factory)
    operator_repository = SAOperatorProfileRepository(session_factory)
    character_service = CharacterService(character_repository)

    async def _create_operator(
        *, email: str, display_name: str, timezone_id: str, password: str,
    ) -> str:
        operator = OperatorProfile(
            id=email,  # deterministic, unique per soak operator
            display_name=display_name,
            email=email,
            password_hash="x",
            is_admin=True,
            timezone_id=timezone_id,
        )
        await operator_repository.save(operator)
        return operator.id

    plan = build_seed_plan(
        operators=2,
        characters_per=3,
        freeze_fraction=0.15,          # round(6 * 0.15) = 1 frozen
        proactive_disabled_fraction=0.0,
        seed=42,
    )
    summary = await seed_population(
        plan,
        create_operator=_create_operator,
        character_service=character_service,
        character_repository=character_repository,
        password="soak-test-pw",
        now=BASE,
    )
    assert summary.operators == 2
    assert summary.characters == 6
    assert summary.frozen == 1

    # The frozen character is excluded from list_active, so exactly the 5
    # unfrozen characters will be enqueued + journaled.
    active = await character_repository.list_active()
    assert len(active) == 5
    active_ids = sorted(c.id for c in active)

    queue = SABackgroundJobQueue(session_factory)
    lease = SABackgroundCoordinatorLease(session_factory)
    cursor = SABackgroundCoordinatorCursor(session_factory)
    journal = SATickJournal(session_factory)
    # Two buckets, deliberately spaced by 2 so a per-bucket miss cannot be
    # bridged by a neighbour under ±1 tolerance.
    t0 = BASE
    t2 = BASE + timedelta(seconds=2 * BUCKET)
    b0 = bucket_for(t0, BUCKET)
    b2 = bucket_for(t2, BUCKET)

    # After the Phase 5 §13 cutover the distributed coordinator no longer
    # enqueues per-character character_tick; the shadow soak runs in MIRROR mode
    # (embedded backend + shadow=postgres), where the embedded tick JOURNAL drives
    # the queue. Record both buckets' journals, then mirror them into the queue
    # (one bucket per pass, earliest first) and dry-run-drain — the very set the
    # §14 comparison scores against.
    coordinator = ShadowCoordinator(
        queue=queue, lease=lease, cursor=cursor, journal=journal,
        character_repository=character_repository, owner_id="coord-soak",
        bucket_seconds=BUCKET, clock=_FrozenClock(t2), mirror_from_journal=True,
    )
    worker = ShadowDryRunWorker(
        queue=queue, character_repository=character_repository,
        worker_id="worker-soak", dry_run_hold_seconds=0.0,
    )

    for now, bucket in ((t0, b0), (t2, b2)):
        await journal.record(
            [(bucket, "character_tick", cid) for cid in active_ids]
            + [(bucket, "social_tick", None)],
            now=now,
        )
    # Two mirror passes (frozen at t2 so both buckets are in-window): the first
    # mirrors the earliest journal bucket b0, the second b2.
    for _ in range(2):
        await coordinator.run_once(now=t2)
        await _drain(worker, t2)

    # Window brackets both buckets as interior (boundary grace excludes the
    # outermost bucket on each side).
    from_ts = t0 - timedelta(seconds=BUCKET)
    to_ts = t2 + timedelta(seconds=BUCKET)
    url = _async_url(postgres_container)

    journal_entries, job_rows, window = await load_soak_rows(
        database_url=url, from_ts=from_ts, to_ts=to_ts, bucket_seconds=BUCKET,
    )
    report = compare_shadow(
        journal_entries, job_rows, bucket_seconds=BUCKET, window=window,
    )
    assert report.missed_count == 0
    assert report.duplicate_count == 0
    assert report.wrong_run_count == 0
    assert report.buckets_evaluated >= 2

    # Now delete one completed job (one character at the later bucket). With no
    # neighbouring job within ±1, the judge must report exactly one missed job.
    victim = active_ids[0]
    async with session_factory() as session:
        result = await session.execute(
            delete(BackgroundJobRow).where(
                BackgroundJobRow.idempotency_key
                == f"character_tick:{victim}:{b2}",
            ),
        )
        await session.commit()
        assert result.rowcount == 1

    journal_entries2, job_rows2, window2 = await load_soak_rows(
        database_url=url, from_ts=from_ts, to_ts=to_ts, bucket_seconds=BUCKET,
    )
    report2 = compare_shadow(
        journal_entries2, job_rows2, bucket_seconds=BUCKET, window=window2,
    )
    assert report2.missed_count == 1
    assert report2.missed[0]["character_id"] == victim
    assert report2.missed[0]["bucket"] == b2
    assert report2.duplicate_count == 0
    assert report2.wrong_run_count == 0
