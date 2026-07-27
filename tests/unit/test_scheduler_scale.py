"""Phase 0 scheduler scale/latency test (HOSTED_CORE_SCALING §12 / §13).

Drives ``ProactiveScheduler._tick_all`` over N proactive-enabled characters
with a latency-injecting dispatcher double and asserts the recorded metrics:
ticks increment, the active gauge equals N, and the accumulated
``proactive_dispatch`` step duration scales with N × injected-delay.

That last assertion is the point: the per-character loop dispatches
**serially**, so a slow per-character step multiplies straight through the
character count. This documents the bottleneck that Phase 3 (worker cutover +
due-time jobs) removes by parallelising background work off the single tick.

Deterministic: no real LLM, in-memory repos, an injected asyncio delay small
enough to keep total wall time well under a few seconds even at N=100.
"""

from __future__ import annotations

import asyncio

import pytest

from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.observability.scheduler_metrics import (
    SchedulerMetrics,
)
from tests.unit._messaging_harness import build_messaging_harness, create_character

_DELAY_SECONDS = 0.002  # ~2ms injected per-character dispatch latency


class _DelayInjectingDispatcher:
    """Dispatcher double that sleeps a fixed delay per ``evaluate`` call so the
    serial per-character loop shows up as measurable accumulated time."""

    def __init__(self, delay_seconds: float) -> None:
        self._delay = delay_seconds
        self.calls: list[str] = []

    async def evaluate(
        self, *, character_id: str, trigger: ProactiveTrigger, now=None,
    ) -> None:
        await asyncio.sleep(self._delay)
        self.calls.append(character_id)


async def _run_scale(n: int) -> None:
    harness = build_messaging_harness()
    for i in range(n):
        await create_character(harness, name=f"Char{i}", proactive_enabled=True)

    metrics = SchedulerMetrics()
    dispatcher = _DelayInjectingDispatcher(_DELAY_SECONDS)
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=999.0,
        startup_grace_seconds=0.0,  # allow dispatch immediately
        metrics=metrics,
    )

    await scheduler._tick_all()  # noqa: SLF001 — direct drive is the test seam

    # Every character was dispatched exactly once this tick.
    assert len(dispatcher.calls) == n
    # Counter advanced and the active gauge reflects the working set.
    assert metrics.ticks_total == 1
    assert metrics.characters_active == n
    assert metrics.characters_total == n
    assert metrics.characters_frozen == 0

    dispatch_seconds = metrics.last_tick_step_durations["proactive_dispatch"]
    # Serial loop: accumulated dispatch time tracks N × delay. The 0.8 slack
    # absorbs sleep-granularity jitter while still proving it scales with N
    # (i.e. the tick does NOT parallelise — the Phase 3 target).
    assert dispatch_seconds >= n * _DELAY_SECONDS * 0.8
    # The whole tick is at least as long as its largest single step.
    assert metrics.last_tick_duration_seconds >= dispatch_seconds


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [25, 50, 100])
async def test_serial_dispatch_scales_with_character_count(n: int) -> None:
    await _run_scale(n)
