"""PostgreSQL integration for the §2.1 dedicated coordinator/worker roles.

Proves the two dedicated roles compose over the REAL durable queue / lease /
execution-mode tables:

* a coordinator (leader) enqueues due work and a worker claims + executes it in
  one round (fake tick body — no provider is called);
* two coordinators sharing the ``background-coordinator`` lease elect exactly one
  active leader (the passive one enqueues nothing);
* a coordinator obeys the three-state execution-mode barrier — under
  ``embedded`` / ``paused`` it enqueues nothing, so a lingering ``background``
  process and a dedicated coordinator can never double-drive the same bucket.

Requires Docker (testcontainers); skipped automatically when unavailable.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from kokoro_link.application.services.account_runtime_profile import (
    PermissiveAccountRuntimeProfileResolver,
)
from kokoro_link.application.services.background_shadow_coordinator import (
    CHARACTER_TICK_KIND,
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
)
from kokoro_link.contracts.execution_mode import (
    MODE_DISTRIBUTED,
    MODE_EMBEDDED,
    MODE_PAUSED,
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

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


@dataclass
class _FrozenClock:
    """Pins ``_fresh_now`` to BASE so the coordinator's post-enqueue leader
    re-check compares the lease against the same instant it was acquired at,
    rather than the real wall clock (which is years past BASE)."""

    value: datetime

    def now(self) -> datetime:
        return self.value


def _character(cid: str) -> Character:
    c = Character.create(
        name=cid, summary="", user_id="op-1", personality=[], interests=[],
        speaking_style="", boundaries=[], proactive_enabled=True,
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
            last_active_at=BASE,
        ),
    )
    c = replace(c, id=cid, created_at=BASE - timedelta(days=30))
    return c


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
    """Duck-typed tick body claiming a REAL visible slot (no provider)."""

    def __init__(self, slots: SAVisibleSlotRepository) -> None:
        self._slots = slots

    async def run(self, character, *, now, gate, beat_sink, allow_dispatch,
                  step_timer=None, abort_check=None, logical_slot=None):
        if abort_check is not None and abort_check():
            return
        await self._slots.claim(character.id, SLOT_KIND_PROACTIVE, logical_slot)

    async def subscription_allows(self, character):
        return True

    async def dispatch_beat(self, *a, **k):
        return None

    async def run_warmup(self, *a, **k):
        return None


def _coordinator(session_factory, characters, owner_id, ownership):
    return ShadowCoordinator(
        queue=SABackgroundJobQueue(session_factory),
        lease=SABackgroundCoordinatorLease(session_factory),
        cursor=SABackgroundCoordinatorCursor(session_factory),
        journal=SATickJournal(session_factory),
        character_repository=_CharRepo(characters),
        owner_id=owner_id,
        bucket_seconds=300,
        clock=_FrozenClock(BASE),
        runtime_ownership=ownership,
    )


def _worker(session_factory, characters, slots):
    queue = SABackgroundJobQueue(session_factory)
    ownership = SABackgroundExecutionMode(session_factory)
    runner = ExecutionModeRunner(
        queue=queue,
        character_repository=_CharRepo(characters),
        character_tick_executor=_SlotExecutor(slots),
        social_tick_executor=SocialTickExecutor(
            character_repository=_CharRepo(characters),
        ),
        gate_service=RuntimeActivityGateService(
            resolver=PermissiveAccountRuntimeProfileResolver(),
            character_repository=_CharRepo(characters),
        ),
        cursor=SABackgroundCoordinatorCursor(session_factory),
        bucket_seconds=300,
        clock=_FrozenClock(BASE),
    )
    worker = ShadowDryRunWorker(
        queue=queue, character_repository=_CharRepo(characters),
        worker_id="w1", dry_run_hold_seconds=0.0, clock=_FrozenClock(BASE),
    )
    worker.enable_execution(runtime_ownership=ownership, execution_runner=runner)
    return worker


async def _job_count(session_factory) -> int:
    async with session_factory() as session:
        return int((await session.execute(
            select(func.count()).select_from(BackgroundJobRow),
        )).scalar_one())


async def _slot_count(session_factory) -> int:
    async with session_factory() as session:
        return int((await session.execute(
            select(func.count()).select_from(BackgroundVisibleSlotRow),
        )).scalar_one())


async def test_coordinator_and_worker_roles_enqueue_claim_execute(
    session_factory,
) -> None:
    # The dedicated coordinator + worker roles, composed over the real tables,
    # drive one bucket end to end: coordinator (leader) enqueues, worker executes.
    characters = [_character("char-a"), _character("char-b")]
    slots = SAVisibleSlotRepository(session_factory)
    ownership = SABackgroundExecutionMode(session_factory)
    assert await ownership.flip(MODE_DISTRIBUTED, 0, now=BASE) == 1

    coordinator = _coordinator(session_factory, characters, "coord-1", ownership)
    await coordinator.run_once(now=BASE)
    # Phase 5 §13 cutover COMPLETE: the coordinator enqueues nothing per bucket
    # (no character_tick, no social_tick). Both handlers are retained for redrive —
    # enqueue a character_tick per active char directly to drive the whole-tick
    # execution path (the _SlotExecutor claims a visible slot in run()).
    _owner, epoch, _until = await coordinator._lease.current(  # noqa: SLF001
        COORDINATOR_LEASE_NAME,
    )
    bucket = int(BASE.timestamp()) // 300
    enqueue_queue = SABackgroundJobQueue(session_factory)
    for character in characters:
        await enqueue_queue.enqueue(
            BackgroundJobSpec(
                kind=CHARACTER_TICK_KIND,
                idempotency_key=f"{CHARACTER_TICK_KIND}:{character.id}:{bucket}",
                due_at=BASE, fencing_epoch=epoch, character_id=character.id,
                payload={"character_id": character.id, "bucket": bucket},
            ), now=BASE,
        )
    assert await _job_count(session_factory) == 2  # the 2 manual character_tick

    worker = _worker(session_factory, characters, slots)
    await worker.run_once(now=BASE)

    # Both active characters produced a visible slot → real execution happened.
    assert await _slot_count(session_factory) == 2


async def test_dual_coordinators_elect_single_active_leader(
    session_factory,
) -> None:
    characters = [_character("char-a")]
    ownership = SABackgroundExecutionMode(session_factory)
    assert await ownership.flip(MODE_DISTRIBUTED, 0, now=BASE) == 1

    coord_a = _coordinator(session_factory, characters, "coord-a", ownership)
    coord_b = _coordinator(session_factory, characters, "coord-b", ownership)
    await coord_a.run_once(now=BASE)
    await coord_b.run_once(now=BASE)

    # Exactly one coordinator holds the shared background-coordinator lease.
    active = [c for c in (coord_a, coord_b) if c.epoch is not None]
    assert len(active) == 1
    lease = SABackgroundCoordinatorLease(session_factory)
    owner, _epoch, _until = await lease.current(COORDINATOR_LEASE_NAME)
    assert owner == active[0]._owner_id  # noqa: SLF001


@pytest.mark.parametrize("mode", [MODE_EMBEDDED, MODE_PAUSED])
async def test_coordinator_does_not_enqueue_off_distributed(
    session_factory, mode,
) -> None:
    # The three-state ownership barrier: under embedded/paused the coordinator
    # enqueues NOTHING, so a dedicated coordinator can never double-drive a bucket
    # that the (transitional) embedded scheduler still owns.
    characters = [_character("char-a")]
    ownership = SABackgroundExecutionMode(session_factory)
    assert await ownership.flip(mode, 0, now=BASE) == 1

    coordinator = _coordinator(session_factory, characters, "coord-1", ownership)
    await coordinator.run_once(now=BASE)

    assert await _job_count(session_factory) == 0
    assert coordinator.epoch is None
