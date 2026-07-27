from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.execution_mode_transition import (
    DRAIN_CURSOR_NAME,
    ExecutionModeTransitionService,
)
from kokoro_link.contracts.execution_mode import MODE_DISTRIBUTED, MODE_PAUSED
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorCursor,
    InMemoryRuntimeOwnership,
)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class _Queue:
    def __init__(self, claimed: int = 0) -> None:
        self.claimed = claimed

    async def stats(self, *, now):
        class Stats:
            status_counts = {"claimed": self.claimed}

        return Stats()


@pytest.mark.asyncio
async def test_transition_service_requires_durable_two_observation_barrier() -> None:
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    clock = _Clock(now)
    ownership = InMemoryRuntimeOwnership()
    cursor = InMemoryBackgroundCoordinatorCursor()
    service = ExecutionModeTransitionService(
        ownership=ownership,
        queue=_Queue(),
        cursor=cursor,
        clock=clock,
        min_observation_interval=1.0,
    )

    entered = await service.enter_paused(expected_epoch=0)
    assert entered.ok and entered.epoch == 1
    assert (await service.leave_paused(MODE_DISTRIBUTED, expected_epoch=1)).code == "drain_not_confirmed"
    first = await service.observe_drain(paused_epoch=1)
    assert first.ok and not first.confirmed
    clock.value += timedelta(seconds=0.5)
    too_soon = await service.observe_drain(paused_epoch=1)
    assert too_soon.ok and not too_soon.confirmed
    clock.value += timedelta(seconds=1.0)
    second = await service.observe_drain(paused_epoch=1)
    assert second.ok and second.confirmed
    left = await service.leave_paused(MODE_DISTRIBUTED, expected_epoch=1)
    assert left.ok and left.epoch == 2
    assert await cursor.get(DRAIN_CURSOR_NAME) == {}


@pytest.mark.asyncio
async def test_transition_service_resets_proof_when_claimed_and_rejects_stale_epoch() -> None:
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)
    clock = _Clock(now)
    ownership = InMemoryRuntimeOwnership()
    cursor = InMemoryBackgroundCoordinatorCursor()
    queue = _Queue()
    service = ExecutionModeTransitionService(
        ownership=ownership,
        queue=queue,
        cursor=cursor,
        clock=clock,
        min_observation_interval=0,
    )
    await service.enter_paused(expected_epoch=0)
    await service.observe_drain(paused_epoch=1)
    clock.value += timedelta(seconds=1)
    assert (await service.observe_drain(paused_epoch=1)).confirmed
    queue.claimed = 1
    reset = await service.observe_drain(paused_epoch=1)
    assert reset.ok and not reset.confirmed
    assert (await service.leave_paused(MODE_DISTRIBUTED, expected_epoch=2)).code == "epoch_mismatch"
    assert (await service.leave_paused(MODE_DISTRIBUTED, expected_epoch=1)).code == "drain_not_confirmed"
    assert (await ownership.get()) == (MODE_PAUSED, 1)
