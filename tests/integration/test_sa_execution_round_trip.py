"""PostgreSQL integration for distributed execution (HOSTED_CORE_SCALING §13 P3-C).

Proves the execution path end-to-end against REAL queue / execution-mode /
visible-slot tables (the in-memory service harness pattern for the tick body, per
§13): the coordinator enqueues, an execution-mode worker claims + executes,
visible-slot rows are produced for active characters, jobs complete
``executed:true``, a frozen character's job is completed skipped (not executed),
and a reclaimed job racing the same slot yields exactly ONE visible-slot row.

Requires Docker (testcontainers); skipped automatically when unavailable.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from kokoro_link.application.services.account_runtime_profile import (
    PermissiveAccountRuntimeProfileResolver,
)
from kokoro_link.application.services.background_shadow_coordinator import (
    CHARACTER_TICK_KIND,
    SOCIAL_TICK_KIND,
    ShadowCoordinator,
)
from kokoro_link.application.services.background_shadow_worker import (
    ShadowDryRunWorker,
)
from kokoro_link.application.services.execution_mode_runner import (
    ExecutionModeRunner,
)
from kokoro_link.application.services.runtime_activity_gate import (
    RuntimeActivityGateService,
)
from kokoro_link.application.services.social_tick_executor import (
    SocialTickExecutor,
)
from kokoro_link.contracts.background_jobs import (
    COORDINATOR_LEASE_NAME,
    BackgroundJobSpec,
    JobStatus,
)
from kokoro_link.contracts.visible_slots import SLOT_KIND_PROACTIVE
from kokoro_link.domain.entities.character import Character, CharacterState
from kokoro_link.infrastructure.persistence.models import (
    BackgroundJobRow,
    BackgroundVisibleSlotRow,
)
from kokoro_link.infrastructure.persistence.sa_background_jobs import (
    SABackgroundJobQueue,
)
from kokoro_link.infrastructure.persistence.sa_background_runtime import (
    SABackgroundCoordinatorCursor,
    SABackgroundCoordinatorLease,
    SABackgroundExecutionMode,
    SATickJournal,
)
from kokoro_link.infrastructure.persistence.sa_visible_slots import (
    SAVisibleSlotRepository,
)
from kokoro_link.contracts.execution_mode import MODE_DISTRIBUTED

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
BUCKET = int(BASE.timestamp()) // 300


class _FrozenClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def _character(cid: str, *, frozen: bool = False) -> Character:
    c = Character.create(
        name=cid, summary="", user_id="op-1", personality=[], interests=[],
        speaking_style="", boundaries=[], proactive_enabled=True,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=BASE,
        ),
    )
    c = replace(c, id=cid, created_at=BASE - timedelta(days=30))
    return replace(c, frozen=frozen) if frozen else c


class _CharRepo:
    def __init__(self, characters: list[Character]) -> None:
        self._characters = characters

    async def get(self, character_id: str):
        return next((c for c in self._characters if c.id == character_id), None)

    async def list_active(self):
        return [c for c in self._characters if not c.frozen]

    async def list(self):
        return list(self._characters)


class _SlotExecutor:
    """Duck-typed tick body that claims a REAL visible slot (stands in for the
    provider-bound CharacterTickExecutor)."""

    def __init__(self, slots: SAVisibleSlotRepository) -> None:
        self._slots = slots
        self.slot_claims: list[tuple[str, bool]] = []

    async def run(self, character, *, now, gate, beat_sink, allow_dispatch,
                  step_timer=None, abort_check=None, logical_slot=None):
        if abort_check is not None and abort_check():
            return
        won = await self._slots.claim(
            character.id, SLOT_KIND_PROACTIVE, logical_slot,
        )
        self.slot_claims.append((character.id, won))

    async def subscription_allows(self, character):
        return True

    async def dispatch_beat(self, *a, **k):
        return None

    async def run_warmup(self, *a, **k):
        return None


async def _slot_count(session_factory) -> int:
    async with session_factory() as session:
        return int((await session.execute(
            select(func.count()).select_from(BackgroundVisibleSlotRow),
        )).scalar_one())


async def _jobs_by_kind(session_factory):
    async with session_factory() as session:
        rows = (await session.execute(select(BackgroundJobRow))).scalars().all()
    return list(rows)


def _outcome(row) -> dict:
    raw = row.outcome_json
    return json.loads(raw) if isinstance(raw, str) else (raw or {})


def _build_worker(session_factory, characters, slots):
    queue = SABackgroundJobQueue(session_factory)
    ownership = SABackgroundExecutionMode(session_factory)
    char_exec = _SlotExecutor(slots)
    runner = ExecutionModeRunner(
        queue=queue,
        character_repository=_CharRepo(characters),
        character_tick_executor=char_exec,
        social_tick_executor=SocialTickExecutor(
            character_repository=_CharRepo(characters),
        ),
        gate_service=RuntimeActivityGateService(
            resolver=PermissiveAccountRuntimeProfileResolver(),
            character_repository=_CharRepo(characters),
        ),
        cursor=SABackgroundCoordinatorCursor(session_factory),
        bucket_seconds=300,
    )
    worker = ShadowDryRunWorker(
        queue=queue, character_repository=_CharRepo(characters),
        worker_id="w1", dry_run_hold_seconds=0.0,
    )
    worker.enable_execution(runtime_ownership=ownership, execution_runner=runner)
    return worker, queue, char_exec


async def test_execution_round_trip_active_slots_and_frozen_skip(
    session_factory,
) -> None:
    characters = [
        _character("char-a"), _character("char-b"),
        _character("char-frozen", frozen=True),
    ]
    slots = SAVisibleSlotRepository(session_factory)

    # Distributed mode ON.
    assert await SABackgroundExecutionMode(session_factory).flip(
        MODE_DISTRIBUTED, 0, now=BASE,
    ) == 1

    # The coordinator pass no longer enqueues per-bucket work (§13); it is run
    # only to acquire the leader lease whose epoch fences the manual enqueues below.
    lease = SABackgroundCoordinatorLease(session_factory)
    coordinator = ShadowCoordinator(
        queue=SABackgroundJobQueue(session_factory),
        lease=lease,
        cursor=SABackgroundCoordinatorCursor(session_factory),
        journal=SATickJournal(session_factory),
        character_repository=_CharRepo(characters),
        owner_id="coord-1",
        bucket_seconds=300,
        clock=_FrozenClock(BASE),
    )
    await coordinator.run_once(now=BASE)
    _owner, epoch, _until = await lease.current(COORDINATOR_LEASE_NAME)

    # Phase 5 §13 cutover: the coordinator no longer auto-enqueues per-character
    # character_tick, but the character_tick handler is RETAINED for redrive of a
    # legacy queued row. This test exercises that retained whole-tick execution
    # path directly: enqueue a character_tick for each active char + the frozen
    # one (frozen while queued → the worker's validate must skip it).
    enqueue_queue = SABackgroundJobQueue(session_factory)
    for cid in ("char-a", "char-b", "char-frozen"):
        await enqueue_queue.enqueue(
            BackgroundJobSpec(
                kind=CHARACTER_TICK_KIND,
                idempotency_key=f"{CHARACTER_TICK_KIND}:{cid}:{BUCKET}",
                due_at=BASE, fencing_epoch=epoch, character_id=cid,
                payload={"character_id": cid, "bucket": BUCKET},
            ), now=BASE,
        )
    # §13: social_tick is no longer auto-enqueued, but its handler is RETAINED for
    # redrive of a legacy queued row — enqueue one directly to exercise it.
    await enqueue_queue.enqueue(
        BackgroundJobSpec(
            kind=SOCIAL_TICK_KIND,
            idempotency_key=f"{SOCIAL_TICK_KIND}:{BUCKET}",
            due_at=BASE, fencing_epoch=epoch, character_id=None,
            payload={"bucket": BUCKET},
        ), now=BASE,
    )

    worker, _queue, _exec = _build_worker(session_factory, characters, slots)
    await worker.run_once(now=BASE)

    # Two visible-slot rows: one per active character, frozen produced none.
    assert await _slot_count(session_factory) == 2
    async with session_factory() as session:
        slot_chars = set((await session.execute(
            select(BackgroundVisibleSlotRow.character_id),
        )).scalars().all())
    assert slot_chars == {"char-a", "char-b"}

    rows = await _jobs_by_kind(session_factory)
    by_char = {r.character_id: r for r in rows if r.kind == CHARACTER_TICK_KIND}
    assert by_char["char-a"].status == JobStatus.DONE.value
    assert _outcome(by_char["char-a"])["executed"] is True
    assert _outcome(by_char["char-b"])["executed"] is True
    # Frozen: completed but NOT executed (correct skip).
    assert by_char["char-frozen"].status == JobStatus.DONE.value
    assert _outcome(by_char["char-frozen"])["executed"] is False
    assert _outcome(by_char["char-frozen"])["skip_reason"] == "frozen"
    # social_tick executed too.
    social = next(r for r in rows if r.kind == "social_tick")
    assert social.status == JobStatus.DONE.value
    assert _outcome(social)["executed"] is True


async def test_reclaimed_job_racing_same_slot_yields_one_row(
    session_factory,
) -> None:
    # Two runs of the SAME (char, kind, slot) — an original and a reclaimed
    # distributed twin — produce exactly ONE visible-slot row (the ledger's
    # at-most-once claim), never two visible outputs.
    characters = [_character("char-a")]
    slots = SAVisibleSlotRepository(session_factory)
    assert await SABackgroundExecutionMode(session_factory).flip(
        MODE_DISTRIBUTED, 0, now=BASE,
    ) == 1
    lease = SABackgroundCoordinatorLease(session_factory)
    assert await lease.acquire(
        COORDINATOR_LEASE_NAME, "coord", ttl_seconds=300, now=BASE,
    ) == 1
    queue = SABackgroundJobQueue(session_factory)
    await queue.enqueue(
        BackgroundJobSpec(
            kind=CHARACTER_TICK_KIND,
            idempotency_key=f"{CHARACTER_TICK_KIND}:char-a:{BUCKET}",
            due_at=BASE, fencing_epoch=1, character_id="char-a",
            payload={"character_id": "char-a", "bucket": BUCKET},
        ), now=BASE,
    )

    # Worker A claims with a SHORT lease and completes (one slot claim).
    worker_a, _q, exec_a = _build_worker(session_factory, characters, slots)
    worker_a._lease_seconds = 1  # noqa: SLF001
    worker_a._execution_lease_seconds = 1  # noqa: SLF001
    await worker_a.run_once(now=BASE)
    assert exec_a.slot_claims == [("char-a", True)]  # A won the slot

    # Re-enqueue the SAME logical job (a fresh bucket occurrence / redrive) and
    # let worker B race the same slot id — B's claim loses (row already exists).
    await queue.enqueue(
        BackgroundJobSpec(
            kind=CHARACTER_TICK_KIND,
            idempotency_key=f"{CHARACTER_TICK_KIND}:char-a:{BUCKET}",
            due_at=BASE, fencing_epoch=1, character_id="char-a",
            payload={"character_id": "char-a", "bucket": BUCKET},
        ), now=BASE + timedelta(seconds=5),
    )
    worker_b, _q2, exec_b = _build_worker(session_factory, characters, slots)
    await worker_b.run_once(now=BASE + timedelta(seconds=5))
    assert exec_b.slot_claims == [("char-a", False)]  # B lost the slot claim

    # Exactly one visible-slot row survived the race.
    assert await _slot_count(session_factory) == 1
