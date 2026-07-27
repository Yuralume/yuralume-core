"""Embedded execution-ownership gating in ProactiveScheduler (P3-B, §13/§15).

Matrix:
* NOT enforced → the ownership port is NEVER touched (a raising stub proves the
  self-host red line: zero reads).
* enforced + mode=='embedded' → the tick runs normally.
* enforced + mode=='distributed' → the whole tick body is skipped (no dispatch,
  no metrics tick) and a skip is counted.
* enforced + the read raises → fail-closed skip + counted.
* the pause warning is throttled (once per state change, not per tick).
* the per-event gate skips dispatch too (start → notify_event → no dispatch).
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.contracts.execution_mode import (
    MODE_DISTRIBUTED,
    MODE_EMBEDDED,
)
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.observability.scheduler_metrics import (
    SchedulerMetrics,
)
from tests.unit._messaging_harness import build_messaging_harness, create_character

pytestmark = pytest.mark.asyncio


class _RecordingDispatcher:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def evaluate(
        self, *, character_id: str, trigger: ProactiveTrigger, now=None,
    ) -> None:
        self.calls.append(character_id)


class _FixedOwnership:
    """Ownership stub returning a fixed mode; counts reads."""

    def __init__(self, mode: str) -> None:
        self._mode = mode
        self.reads = 0

    async def get(self) -> tuple[str, int]:
        self.reads += 1
        return (self._mode, 3)

    async def flip(self, target_mode, expected_epoch, *, now):  # pragma: no cover
        raise AssertionError("flip must not be called by the gate")


class _RaisingOwnership:
    """Ownership stub whose get() raises — the never-called + fail-closed probe."""

    def __init__(self) -> None:
        self.reads = 0

    async def get(self) -> tuple[str, int]:
        self.reads += 1
        raise RuntimeError("ownership backend down")

    async def flip(self, target_mode, expected_epoch, *, now):  # pragma: no cover
        raise AssertionError("flip must not be called by the gate")


async def _build(mode_or_port, *, enforced: bool):
    harness = build_messaging_harness()
    await create_character(harness, name="A", proactive_enabled=True)
    await create_character(harness, name="B", proactive_enabled=True)
    metrics = SchedulerMetrics()
    dispatcher = _RecordingDispatcher()
    scheduler = ProactiveScheduler(
        dispatcher=dispatcher,  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=999.0,
        startup_grace_seconds=0.0,
        metrics=metrics,
        runtime_ownership=mode_or_port,
        ownership_enforced=enforced,
    )
    return scheduler, dispatcher, metrics


async def test_not_enforced_never_reads_port() -> None:
    port = _RaisingOwnership()  # would raise if ever read
    scheduler, dispatcher, metrics = await _build(port, enforced=False)
    await scheduler._tick_all()  # noqa: SLF001
    assert port.reads == 0  # the red line: zero ownership reads
    assert metrics.ticks_total == 1
    assert len(dispatcher.calls) == 2
    assert scheduler.ownership_skips_total == 0


async def test_enforced_embedded_runs_tick() -> None:
    port = _FixedOwnership(MODE_EMBEDDED)
    scheduler, dispatcher, metrics = await _build(port, enforced=True)
    await scheduler._tick_all()  # noqa: SLF001
    assert port.reads == 1
    assert metrics.ticks_total == 1
    assert len(dispatcher.calls) == 2
    assert scheduler.ownership_skips_total == 0


async def test_enforced_distributed_skips_tick_and_executors() -> None:
    port = _FixedOwnership(MODE_DISTRIBUTED)
    scheduler, dispatcher, metrics = await _build(port, enforced=True)
    await scheduler._tick_all()  # noqa: SLF001
    assert metrics.ticks_total == 0  # tick body never ran
    assert dispatcher.calls == []  # executors never called
    assert scheduler.ownership_skips_total == 1


async def test_enforced_read_raises_fails_closed_and_counts() -> None:
    port = _RaisingOwnership()
    scheduler, dispatcher, metrics = await _build(port, enforced=True)
    await scheduler._tick_all()  # noqa: SLF001
    assert port.reads == 1
    assert metrics.ticks_total == 0  # fail-closed: skipped
    assert dispatcher.calls == []
    assert scheduler.ownership_skips_total == 1


async def test_pause_warning_throttled_once_per_state_change(caplog) -> None:
    port = _FixedOwnership(MODE_DISTRIBUTED)
    scheduler, _dispatcher, _metrics = await _build(port, enforced=True)
    with caplog.at_level(
        logging.WARNING,
        logger="kokoro_link.application.services.proactive_scheduler",
    ):
        await scheduler._tick_all()  # noqa: SLF001
        await scheduler._tick_all()  # noqa: SLF001
        await scheduler._tick_all()  # noqa: SLF001
    warnings = [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "pausing embedded" in r.getMessage()
    ]
    assert len(warnings) == 1  # throttled: 3 skipped ticks → 1 warning
    assert scheduler.ownership_skips_total == 3


async def test_resume_after_pause_warns_again_on_next_pause() -> None:
    # embedded → distributed → embedded → distributed re-arms the throttle.
    port = _FixedOwnership(MODE_DISTRIBUTED)
    scheduler, _dispatcher, _metrics = await _build(port, enforced=True)
    await scheduler._tick_all()  # noqa: SLF001 — pause
    port._mode = MODE_EMBEDDED
    await scheduler._tick_all()  # noqa: SLF001 — resume
    assert scheduler._ownership_paused is False  # noqa: SLF001
    port._mode = MODE_DISTRIBUTED
    await scheduler._tick_all()  # noqa: SLF001 — pause again
    assert scheduler._ownership_paused is True  # noqa: SLF001
    assert scheduler.ownership_skips_total == 2


async def test_event_dispatch_skipped_when_distributed() -> None:
    port = _FixedOwnership(MODE_DISTRIBUTED)
    scheduler, dispatcher, _metrics = await _build(port, enforced=True)
    await scheduler.start()
    try:
        scheduler.notify_event(
            character_id="A", trigger=ProactiveTrigger.ARC_BEAT,
        )
        # Let the loop pick up the event and hit gate (b).
        for _ in range(50):
            await asyncio.sleep(0.01)
            if scheduler.ownership_skips_total >= 2:
                break
    finally:
        await scheduler.stop()
    # Initial tick + the event were both skipped; nothing dispatched.
    assert dispatcher.calls == []
    assert scheduler.ownership_skips_total >= 2
