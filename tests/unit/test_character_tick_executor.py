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


def _executor_with_recorder(
    rec: _Recorder, *, crash_step: str | None = None, drift=None,  # noqa: ANN001
    goal_review=None,  # noqa: ANN001
    scene_timeout=None,  # noqa: ANN001
):
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
        schedule_weather_drift=drift,
        goal_review_service=goal_review,
        story_scene_timeout_closer=scene_timeout,
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


class _DriftRecorder:
    """Weather-drift service double: records the characters/instants it vetted."""

    def __init__(self, *, crash: bool = False) -> None:
        self.calls: list[tuple[str, datetime | None]] = []
        self._crash = crash

    async def vet(self, character, *, now=None):  # noqa: ANN001
        self.calls.append((character.id, now))
        if self._crash:
            raise RuntimeError("drift down")
        return None


def _ensure_only_executor(rec: _Recorder, drift: _DriftRecorder, **kwargs):
    """Executor whose only wired collaborators are schedule-ensure + drift."""

    class _Schedules:
        async def ensure_window(self, character, **_):  # noqa: ANN001
            rec.order.append("ensure_window")
            if kwargs.get("ensure_crashes"):
                raise RuntimeError("ensure down")

    return CharacterTickExecutor(
        schedule_service=_Schedules(),
        schedule_weather_drift=drift,
        dispatcher=object(),
    )


@pytest.mark.asyncio
async def test_weather_drift_vet_runs_after_ensure_window() -> None:
    rec = _Recorder()
    drift = _DriftRecorder()
    executor = _ensure_only_executor(rec, drift)
    await executor.run(
        _Character(proactive_enabled=False),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
    )
    # The vet reads the day the ensure just materialised, so ordering matters.
    assert rec.order == ["ensure_window"]
    assert drift.calls == [("char-1", NOW)]


@pytest.mark.asyncio
async def test_weather_drift_vet_crash_does_not_break_the_tick() -> None:
    rec = _Recorder()
    executor = _executor_with_recorder(rec, drift=_DriftRecorder(crash=True))
    await executor.run(
        _Character(),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
    )
    assert len(rec.order) == 7


@pytest.mark.asyncio
async def test_weather_drift_vet_still_runs_when_ensure_window_crashes() -> None:
    # The day may already be planned from an earlier tick — a failed ensure
    # must not cost today's correction.
    rec = _Recorder()
    drift = _DriftRecorder()
    executor = _ensure_only_executor(rec, drift, ensure_crashes=True)
    await executor.run(
        _Character(proactive_enabled=False),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
    )
    assert drift.calls == [("char-1", NOW)]


@pytest.mark.asyncio
async def test_schedule_maintenance_step_does_not_carry_the_vet() -> None:
    # ``schedule_maintenance`` is a DAILY chain. Piggybacking the intra-day vet
    # on it would give the distributed topology exactly the once-a-midnight
    # cadence the drift correction exists to replace, so the two are separate
    # steps behind separate kinds.
    rec = _Recorder()
    drift = _DriftRecorder()
    executor = _ensure_only_executor(rec, drift)
    await executor.step_schedule_maintenance(_Character())
    assert rec.order == ["ensure_window"]
    assert drift.calls == []


@pytest.mark.asyncio
async def test_weather_vet_step_runs_the_vet_without_replanning() -> None:
    # The ``schedule_weather_vet`` kind fires on its own sub-daily cadence; it
    # only re-reads today against the current sky, never re-runs the planner.
    rec = _Recorder()
    drift = _DriftRecorder()
    executor = _ensure_only_executor(rec, drift)
    await executor.step_schedule_weather_vet(_Character(), now=NOW)
    assert rec.order == []
    assert drift.calls == [("char-1", NOW)]


@pytest.mark.asyncio
async def test_weather_vet_step_survives_a_crashing_vet() -> None:
    rec = _Recorder()
    executor = _ensure_only_executor(rec, _DriftRecorder(crash=True))
    await executor.step_schedule_weather_vet(_Character(), now=NOW)


@pytest.mark.asyncio
async def test_warmup_skips_the_weather_vet() -> None:
    # A brand-new character's day was planned against this exact sky
    # milliseconds ago — paying for a drift verdict on it is pure waste.
    rec = _Recorder()
    drift = _DriftRecorder()
    executor = _ensure_only_executor(rec, drift)
    await executor.run_warmup(_Character(), now=NOW)
    assert rec.order == ["ensure_window"]
    assert drift.calls == []


@pytest.mark.asyncio
async def test_abort_between_ensure_and_vet_stops_before_the_write() -> None:
    # A reclaimed job's original runner must not land a schedule write after
    # its lease is gone; the vet is the one step in (d) that persists.
    rec = _Recorder()
    drift = _DriftRecorder()
    executor = _ensure_only_executor(rec, drift)
    def abort_check() -> bool:
        # The lease is lost mid-step: every pre-step check passed, the ensure
        # ran, and only then does the runner discover it no longer owns the job.
        return "ensure_window" in rec.order

    await executor.run(
        _Character(proactive_enabled=False),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
        abort_check=abort_check,
    )
    assert rec.order == ["ensure_window"]
    assert drift.calls == []


@pytest.mark.asyncio
async def test_unwired_weather_drift_leaves_the_tick_untouched() -> None:
    rec = _Recorder()
    executor = _executor_with_recorder(rec)
    await executor.run(
        _Character(),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
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


class _GoalReviewRecorder:
    """Daily goal-review service double (CF2)."""

    def __init__(self, *, crash: bool = False) -> None:
        self.calls: list[tuple[str, datetime | None]] = []
        self._crash = crash

    async def review_if_due(self, character, *, now=None) -> bool:  # noqa: ANN001
        self.calls.append((character.id, now))
        if self._crash:
            raise RuntimeError("review down")
        return True


@pytest.mark.asyncio
async def test_goal_review_runs_in_the_embedded_tick() -> None:
    # CF2: a self-host character must converge its goal list too, so the daily
    # review is part of the embedded tick body — not only the distributed
    # ``goal_review`` chain. The service's own per-day claim decides whether
    # this call costs an LLM round trip.
    rec = _Recorder()
    review = _GoalReviewRecorder()
    executor = _executor_with_recorder(rec, goal_review=review)
    steps: list[str] = []

    async def step_timer(name, coro):  # noqa: ANN001
        steps.append(name)
        return await coro

    await executor.run(
        _Character(),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
        step_timer=step_timer,
    )
    assert review.calls == [("char-1", NOW)]
    # Placed after memorialization, before the gated feed pass.
    assert steps.index("goal_review") == steps.index("memorialize") + 1
    assert steps.index("goal_review") < steps.index("feed_compose")


@pytest.mark.asyncio
async def test_goal_review_crash_does_not_stop_the_tick() -> None:
    rec = _Recorder()
    executor = _executor_with_recorder(
        rec, goal_review=_GoalReviewRecorder(crash=True),
    )
    await executor.run(
        _Character(),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
    )
    assert "proactive_dispatch" in rec.order


@pytest.mark.asyncio
async def test_unwired_goal_review_leaves_the_tick_untouched() -> None:
    # No service → no step, no metric name. Deployments that predate CF2 keep
    # the exact tick body they had.
    rec = _Recorder()
    steps: list[str] = []

    async def step_timer(name, coro):  # noqa: ANN001
        steps.append(name)
        return await coro

    await _executor_with_recorder(rec).run(
        _Character(),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
        step_timer=step_timer,
    )
    assert "goal_review" not in steps


@pytest.mark.asyncio
async def test_step_goal_review_is_the_distributed_entry_point() -> None:
    review = _GoalReviewRecorder()
    executor = CharacterTickExecutor(
        goal_review_service=review, dispatcher=object(),
    )
    await executor.step_goal_review(_Character(), now=NOW)
    assert review.calls == [("char-1", NOW)]


@pytest.mark.asyncio
async def test_step_goal_review_is_fail_soft() -> None:
    executor = CharacterTickExecutor(
        goal_review_service=_GoalReviewRecorder(crash=True), dispatcher=object(),
    )
    await executor.step_goal_review(_Character(), now=NOW)  # must not raise


class _SceneTimeoutRecorder:
    """Stands in for ``StorySceneTimeoutCloser`` at its one step seam."""

    def __init__(self, *, crash: bool = False) -> None:
        self.calls: list[tuple[str, datetime]] = []
        self._crash = crash

    async def close_if_idle(self, character, *, now=None) -> bool:  # noqa: ANN001
        self.calls.append((character.id, now))
        if self._crash:
            raise RuntimeError("scene close blew up")
        return True


@pytest.mark.asyncio
async def test_scene_timeout_runs_in_the_embedded_tick() -> None:
    # SC1-E red line: the per-kind registry is distributed-only, so hanging the
    # wrap-up on that chain alone would mean a self-host player who walked away
    # mid-scene never gets out of it. The embedded tick must run it too.
    rec = _Recorder()
    closer = _SceneTimeoutRecorder()
    executor = _executor_with_recorder(rec, scene_timeout=closer)
    steps: list[str] = []

    async def step_timer(name, coro):  # noqa: ANN001
        steps.append(name)
        return await coro

    await executor.run(
        _Character(),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
        step_timer=step_timer,
    )
    assert closer.calls == [("char-1", NOW)]
    # Before the visible feed / proactive steps, so a scene closed this tick
    # immediately un-pauses the character's proactive delivery.
    assert steps.index("story_scene_timeout") < steps.index("feed_compose")
    assert steps.index("story_scene_timeout") < steps.index("proactive_dispatch")


@pytest.mark.asyncio
async def test_scene_timeout_crash_does_not_stop_the_tick() -> None:
    rec = _Recorder()
    executor = _executor_with_recorder(
        rec, scene_timeout=_SceneTimeoutRecorder(crash=True),
    )
    await executor.run(
        _Character(),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
    )
    assert "proactive_dispatch" in rec.order


@pytest.mark.asyncio
async def test_unwired_scene_timeout_leaves_the_tick_untouched() -> None:
    rec = _Recorder()
    steps: list[str] = []

    async def step_timer(name, coro):  # noqa: ANN001
        steps.append(name)
        return await coro

    await _executor_with_recorder(rec).run(
        _Character(),
        now=NOW,
        gate=_AllowGate(),
        beat_sink=lambda cid, bid: None,
        allow_dispatch=True,
        step_timer=step_timer,
    )
    assert "story_scene_timeout" not in steps


@pytest.mark.asyncio
async def test_both_scheduling_lines_share_one_step() -> None:
    # The distributed entry point is the same private step the tick runs —
    # one implementation, so the red line inside the shared close cannot be
    # bypassed by whichever line happens to fire first.
    closer = _SceneTimeoutRecorder()
    executor = CharacterTickExecutor(
        story_scene_timeout_closer=closer, dispatcher=object(),
    )
    await executor.step_scene_timeout(_Character(), now=NOW)
    assert closer.calls == [("char-1", NOW)]


@pytest.mark.asyncio
async def test_step_scene_timeout_is_fail_soft() -> None:
    executor = CharacterTickExecutor(
        story_scene_timeout_closer=_SceneTimeoutRecorder(crash=True),
        dispatcher=object(),
    )
    await executor.step_scene_timeout(_Character(), now=NOW)  # must not raise
