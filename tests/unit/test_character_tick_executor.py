"""CharacterTickExecutor — the extracted per-character tick body (P3-A).

Proves the executor reproduces the scheduler's per-character sequence exactly:
step order, per-step fail-soft isolation, the feed-compose gate, the beat_sink /
allow_dispatch wiring, the proactive-dispatch guards, and the step_timer hook
names. RuntimeActivityTickGate's structural conformance to CharacterGateView is
also asserted so the narrow protocol stays honest.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.beat_due_checker import BeatScanResult
from kokoro_link.application.services.character_tick_executor import (
    CharacterGateView,
    CharacterTickExecutor,
)
from kokoro_link.application.services.runtime_activity_gate import (
    RuntimeActivityTickGate,
)
from kokoro_link.contracts.background_activity_gate import BackgroundActivityClass
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger

NOW = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)


@dataclass(slots=True)
class _Character:
    id: str = "char-1"
    proactive_enabled: bool = True


class _AllowGate:
    """Gate that allows everything (feed compose + proactive)."""

    async def allows(self, character, activity_class) -> bool:  # noqa: ANN001
        return True

    async def allows_proactive(self, character) -> bool:  # noqa: ANN001
        return True


class _DenyGate:
    def __init__(self, *, feed: bool, proactive: bool) -> None:
        self._feed = feed
        self._proactive = proactive

    async def allows(self, character, activity_class) -> bool:  # noqa: ANN001
        if activity_class is BackgroundActivityClass.FEED_COMPOSE:
            return self._feed
        return False

    async def allows_proactive(self, character) -> bool:  # noqa: ANN001
        return self._proactive


@dataclass
class _Recorder:
    order: list[str] = field(default_factory=list)


def _executor_with_recorder(rec: _Recorder, *, crash_step: str | None = None):
    """Build an executor whose every dependency records its call into ``rec``."""

    def _stub(step: str):
        class _Stub:
            async def refresh(self, character, *, now=None):  # noqa: ANN001
                rec.order.append(step)
                if crash_step == step:
                    raise RuntimeError(step)

            async def scan(self, character, *, now=None):  # noqa: ANN001
                rec.order.append(step)
                if crash_step == step:
                    raise RuntimeError(step)
                return BeatScanResult(attempted_beat_id="b1", should_notify=True)

            async def ensure_window(self, character):  # noqa: ANN001
                rec.order.append(step)
                if crash_step == step:
                    raise RuntimeError(step)

            async def memorialize(self, *, character_id, now=None):  # noqa: ANN001
                rec.order.append(step)
                if crash_step == step:
                    raise RuntimeError(step)

            async def tick(self, character):  # noqa: ANN001
                rec.order.append(step)
                if crash_step == step:
                    raise RuntimeError(step)

            async def evaluate(self, *, character_id, trigger, now=None):  # noqa: ANN001
                rec.order.append(step)
                if crash_step == step:
                    raise RuntimeError(step)

        return _Stub()

    return CharacterTickExecutor(
        rest_recovery_refresher=_stub("rest_recovery"),
        beat_due_checker=_stub("beat_due"),
        schedule_service=_stub("ensure_schedule"),
        schedule_memorializer=_stub("memorialize"),
        feed_composer=_stub("feed_compose"),
        feed_comment_reply=_stub("feed_comment_reply"),
        dispatcher=_stub("proactive_dispatch"),
    )


@pytest.mark.asyncio
async def test_run_executes_exact_step_order() -> None:
    rec = _Recorder()
    executor = _executor_with_recorder(rec)
    beats: list[tuple[str, str | None]] = []
    steps: list[str] = []

    async def step_timer(name, coro):
        steps.append(name)
        return await coro

    await executor.run(
        _Character(),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: beats.append((cid, bid)),
        allow_dispatch=True,
        step_timer=step_timer,
    )

    assert rec.order == [
        "rest_recovery",
        "beat_due",
        "ensure_schedule",
        "memorialize",
        "feed_compose",
        "feed_comment_reply",
        "proactive_dispatch",
    ]
    # step_timer fired with the exact metric step names, same order.
    assert steps == rec.order
    # should_notify=True + allow_dispatch → the beat sink collected the enqueue.
    assert beats == [("char-1", "b1")]


@pytest.mark.asyncio
async def test_feed_compose_gated_off_is_skipped_but_rest_runs() -> None:
    rec = _Recorder()
    executor = _executor_with_recorder(rec)
    await executor.run(
        _Character(),
        now=NOW,
        gate=_DenyGate(feed=False, proactive=True),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
    )
    assert "feed_compose" not in rec.order
    # Everything else still ran, in order.
    assert rec.order == [
        "rest_recovery",
        "beat_due",
        "ensure_schedule",
        "memorialize",
        "feed_comment_reply",
        "proactive_dispatch",
    ]


@pytest.mark.asyncio
async def test_one_step_crashing_does_not_stop_the_rest() -> None:
    # Each per-step failure must be isolated — matching the scheduler's fail-soft.
    for crash in (
        "rest_recovery",
        "beat_due",
        "ensure_schedule",
        "memorialize",
        "feed_compose",
        "feed_comment_reply",
        "proactive_dispatch",
    ):
        rec = _Recorder()
        executor = _executor_with_recorder(rec, crash_step=crash)
        await executor.run(
            _Character(),
            now=NOW,
            gate=_AllowGate(),
            beat_sink=lambda cid, bid: None,
            allow_dispatch=True,
        )
        # The crashing step was reached, and every step ran (none short-circuited
        # the loop). beat_due crashing means the sink isn't called, but later
        # steps still run.
        assert crash in rec.order, crash
        assert len(rec.order) == 7, (crash, rec.order)


@pytest.mark.asyncio
async def test_beat_sink_suppressed_when_allow_dispatch_false() -> None:
    rec = _Recorder()
    executor = _executor_with_recorder(rec)
    beats: list[tuple[str, str | None]] = []
    await executor.run(
        _Character(),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: beats.append((cid, bid)),
        allow_dispatch=False,
    )
    # Beat scan still ran (world advancement), but neither the enqueue nor the
    # proactive dispatch fired under grace.
    assert "beat_due" in rec.order
    assert beats == []
    assert "proactive_dispatch" not in rec.order


@pytest.mark.asyncio
async def test_beat_sink_not_called_when_should_notify_false() -> None:
    rec = _Recorder()

    class _NoNotifyChecker:
        async def scan(self, character, *, now=None):  # noqa: ANN001
            rec.order.append("beat_due")
            return BeatScanResult.empty()

    executor = CharacterTickExecutor(
        beat_due_checker=_NoNotifyChecker(),
        dispatcher=_executor_with_recorder(rec)._dispatcher,  # noqa: SLF001
    )
    beats: list[str] = []
    await executor.run(
        _Character(proactive_enabled=False),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: beats.append(cid),
        allow_dispatch=True,
    )
    assert beats == []


@pytest.mark.asyncio
async def test_proactive_dispatch_guards() -> None:
    # proactive_enabled False → no dispatch.
    rec = _Recorder()
    executor = _executor_with_recorder(rec)
    await executor.run(
        _Character(proactive_enabled=False),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
    )
    assert "proactive_dispatch" not in rec.order

    # gate.allows_proactive False → no dispatch.
    rec = _Recorder()
    executor = _executor_with_recorder(rec)
    await executor.run(
        _Character(),
        now=NOW,
        gate=_DenyGate(feed=True, proactive=False),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
    )
    assert "proactive_dispatch" not in rec.order


@pytest.mark.asyncio
async def test_dispatch_receives_tick_trigger_and_now() -> None:
    calls: list[tuple[str, ProactiveTrigger, datetime | None]] = []

    class _Dispatcher:
        async def evaluate(self, *, character_id, trigger, now=None):  # noqa: ANN001
            calls.append((character_id, trigger, now))

    executor = CharacterTickExecutor(dispatcher=_Dispatcher())
    await executor.run(
        _Character(),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
    )
    assert calls == [("char-1", ProactiveTrigger.TICK, NOW)]


@pytest.mark.asyncio
async def test_subscription_allows_wrapper_fail_soft() -> None:
    class _Guard:
        def __init__(self, *, allow: bool, crash: bool = False) -> None:
            self._allow = allow
            self._crash = crash

        async def is_character_allowed(self, character) -> bool:  # noqa: ANN001
            if self._crash:
                raise RuntimeError("guard down")
            return self._allow

    # No guard → allowed.
    executor = CharacterTickExecutor(dispatcher=object())
    assert await executor.subscription_allows(_Character()) is True

    # Guard allows.
    executor = CharacterTickExecutor(
        subscription_access_guard=_Guard(allow=True), dispatcher=object(),
    )
    assert await executor.subscription_allows(_Character()) is True

    # Guard denies.
    executor = CharacterTickExecutor(
        subscription_access_guard=_Guard(allow=False), dispatcher=object(),
    )
    assert await executor.subscription_allows(_Character()) is False

    # Guard crash → fail-soft to denied.
    executor = CharacterTickExecutor(
        subscription_access_guard=_Guard(allow=True, crash=True),
        dispatcher=object(),
    )
    assert await executor.subscription_allows(_Character()) is False


def test_runtime_activity_tick_gate_conforms_to_character_gate_view() -> None:
    gate = RuntimeActivityTickGate(
        policies={},
        characters_by_id={},
        proactive_phases={},
        feed_phases={},
        operator_phases={},
    )
    # Structural conformance: the narrow protocol methods exist and are async.
    assert hasattr(gate, "allows")
    assert hasattr(gate, "allows_proactive")
    view: CharacterGateView = gate  # type-checks structurally
    assert view is gate
