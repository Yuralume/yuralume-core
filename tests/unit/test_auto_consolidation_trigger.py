"""BDD for ``AutoConsolidationTrigger``.

The trigger sits behind ``ChatService`` post-turn memory write. It must
only fire when it's actually useful — the chat latency is dominated by
the LLM reply already, so we don't want a second LLM call happening on
every turn just because the user added one memory.

Rules we nail down here:

- below the threshold → no-op
- at threshold → fires once
- second call inside cooldown → skipped
- second call after cooldown → fires again
- concurrent calls for the same character → only one runs
- consolidation exceptions don't bubble up
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.auto_consolidation_trigger import (
    AutoConsolidationTrigger,
)


@dataclass
class _Clock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now = self.now + delta


class _CountingRepo:
    def __init__(self, counts: dict[str, int]) -> None:
        self._counts = counts
        self.count_calls = 0

    async def count_for_character(self, character_id: str) -> int:
        self.count_calls += 1
        return self._counts.get(character_id, 0)


class _RecordingService:
    def __init__(self, *, delay: float = 0.0, raises: bool = False) -> None:
        self.calls: list[str] = []
        self._delay = delay
        self._raises = raises

    async def consolidate(self, character_id: str, **_: object):
        self.calls.append(character_id)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._raises:
            raise RuntimeError("boom")
        return _Report(
            character_id=character_id,
            dry_run=False,
            decayed=0,
            clusters_found=0,
            clusters_merged=0,
            memories_replaced=0,
            memories_after=0,
        )


@dataclass
class _Report:
    character_id: str
    dry_run: bool
    decayed: int
    clusters_found: int
    clusters_merged: int
    memories_replaced: int
    memories_after: int


def _make_trigger(
    *,
    counts: dict[str, int],
    threshold: int = 10,
    cooldown_hours: float = 1.0,
    raises: bool = False,
    delay: float = 0.0,
) -> tuple[AutoConsolidationTrigger, _RecordingService, _Clock]:
    repo = _CountingRepo(counts)
    service = _RecordingService(delay=delay, raises=raises)
    clock = _Clock(now=datetime(2026, 4, 19, 12, 0, 0, tzinfo=timezone.utc))
    trigger = AutoConsolidationTrigger(
        memory_repository=repo,  # type: ignore[arg-type]
        consolidation_service=service,  # type: ignore[arg-type]
        threshold=threshold,
        cooldown=timedelta(hours=cooldown_hours),
        clock=clock,
    )
    return trigger, service, clock


@pytest.mark.asyncio
async def test_below_threshold_does_not_trigger() -> None:
    trigger, service, _ = _make_trigger(counts={"c1": 5}, threshold=10)

    fired = await trigger.maybe_trigger("c1")

    assert fired is False
    assert service.calls == []


@pytest.mark.asyncio
async def test_at_threshold_triggers_once() -> None:
    trigger, service, _ = _make_trigger(counts={"c1": 10}, threshold=10)

    fired = await trigger.maybe_trigger("c1")

    assert fired is True
    assert service.calls == ["c1"]


@pytest.mark.asyncio
async def test_second_call_within_cooldown_is_skipped() -> None:
    trigger, service, clock = _make_trigger(
        counts={"c1": 100}, threshold=10, cooldown_hours=1.0,
    )

    first = await trigger.maybe_trigger("c1")
    clock.advance(timedelta(minutes=30))
    second = await trigger.maybe_trigger("c1")

    assert first is True
    assert second is False
    assert service.calls == ["c1"]


@pytest.mark.asyncio
async def test_second_call_after_cooldown_triggers_again() -> None:
    trigger, service, clock = _make_trigger(
        counts={"c1": 100}, threshold=10, cooldown_hours=1.0,
    )

    await trigger.maybe_trigger("c1")
    clock.advance(timedelta(hours=2))
    fired = await trigger.maybe_trigger("c1")

    assert fired is True
    assert service.calls == ["c1", "c1"]


@pytest.mark.asyncio
async def test_concurrent_calls_single_flight() -> None:
    trigger, service, _ = _make_trigger(
        counts={"c1": 100}, threshold=10, delay=0.05,
    )

    # Fire four overlapping triggers for the same character. Only one
    # should actually reach the service — the rest observe the lock or
    # the refreshed last-run timestamp and bail out.
    results = await asyncio.gather(*(trigger.maybe_trigger("c1") for _ in range(4)))

    assert results.count(True) == 1
    assert service.calls == ["c1"]


@pytest.mark.asyncio
async def test_different_characters_run_independently() -> None:
    trigger, service, _ = _make_trigger(
        counts={"a": 100, "b": 100}, threshold=10,
    )

    fired_a = await trigger.maybe_trigger("a")
    fired_b = await trigger.maybe_trigger("b")

    assert fired_a is True
    assert fired_b is True
    assert sorted(service.calls) == ["a", "b"]


@pytest.mark.asyncio
async def test_service_exception_is_swallowed() -> None:
    trigger, service, _ = _make_trigger(
        counts={"c1": 100}, threshold=10, raises=True,
    )

    fired = await trigger.maybe_trigger("c1")

    assert fired is False
    assert service.calls == ["c1"]


@pytest.mark.asyncio
async def test_empty_character_id_is_noop() -> None:
    trigger, service, _ = _make_trigger(counts={}, threshold=1)

    fired = await trigger.maybe_trigger("")

    assert fired is False
    assert service.calls == []
