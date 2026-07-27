"""sol review-fix locks: B2 (busy-defer commit failure propagates after the
GENERATED checkpoint) and H7 (lease heartbeat during the LLM run)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.external_chat.turn_support import (
    TurnFencingStale,
    run_with_lease_heartbeat,
)
from kokoro_link.contracts.busy_reply_decider import BusyDecision, BusyReplyMode
from kokoro_link.domain.entities.schedule import ScheduleActivity

from tests.unit.external_chat_turn_harness import build_harness, make_command

pytestmark = pytest.mark.asyncio


# --- B2: a defer commit failure after GENERATED never re-runs the LLM --------


def _busy_activity(busy: float = 0.9) -> ScheduleActivity:
    now = datetime.now(timezone.utc)
    return ScheduleActivity.create(
        start_at=now - timedelta(minutes=30),
        end_at=now + timedelta(minutes=30),
        description="開會",
        category="meeting",
        busy_score=busy,
    )


class _ScriptedDecider:
    def __init__(self, decision: BusyDecision) -> None:
        self._decision = decision

    async def decide(self, **kwargs):
        return self._decision


class _CommitFencedReceipts:
    """Delegates to the real receipt repo but forces ``mark_committed`` to be
    fenced out (False), simulating a commit failure AFTER the GENERATED
    checkpoint the busy-defer branch writes."""

    def __init__(self, inner) -> None:
        self._inner = inner

    async def mark_committed(self, **_kwargs) -> bool:
        return False

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def test_b2_defer_commit_failure_propagates_without_rerunning_llm() -> None:
    decider = _ScriptedDecider(
        BusyDecision(
            mode=BusyReplyMode.BRIEF_DEFER,
            brief_reply="先回你，等等再好好聊",
            defer_until=datetime.now(timezone.utc) + timedelta(minutes=30),
            defer_reason="會議中",
        ),
    )
    harness = await build_harness(
        decider=decider, current_activity=_busy_activity(),
    )
    harness.turn_service._receipts = _CommitFencedReceipts(harness.receipt_repo)

    result = await harness.turn_service.execute(make_command(message="晚餐吃啥"))

    # The commit failure propagated to receipt recovery (safe retry), and the
    # main reply LLM was NEVER run after the GENERATED checkpoint.
    assert result.status_code == 503
    assert harness.model.generate_calls == 0


# --- H7: lease heartbeat while the LLM / tool pipeline runs ------------------


class _FakeTurn:
    def __init__(self, *, interval: float, fail_after: int | None = None) -> None:
        self.beats = 0
        self._interval = interval
        self._fail_after = fail_after

    async def heartbeat(self) -> None:
        self.beats += 1
        if self._fail_after is not None and self.beats >= self._fail_after:
            raise TurnFencingStale("lease lost")

    def heartbeat_interval_seconds(self) -> float:
        return self._interval


async def test_h7_heartbeat_fires_repeatedly_during_long_work() -> None:
    turn = _FakeTurn(interval=0.01)

    async def work() -> str:
        await asyncio.sleep(0.08)  # ~8 heartbeat windows
        return "done"

    result = await run_with_lease_heartbeat(turn, work())

    assert result == "done"
    assert turn.beats >= 2  # refreshed several times, not just once


async def test_h7_lost_lease_aborts_and_cancels_work() -> None:
    turn = _FakeTurn(interval=0.01, fail_after=1)
    cancelled = {"value": False}

    async def work() -> str:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled["value"] = True
            raise
        return "done"  # pragma: no cover

    with pytest.raises(TurnFencingStale):
        await run_with_lease_heartbeat(turn, work())

    assert cancelled["value"] is True
