"""Unit tests for the synthetic-soak interaction driver.

Covers the deterministic action schedule, the run-loop control (duration /
stop-event), and an auth-off smoke of one chat turn + one freeze toggle driven
against the real FastAPI app via httpx ASGITransport (fake LLM registry from the
messaging harness).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from scripts.soak_interaction_driver import (
    RunHealth,
    ScheduledAction,
    SoakDriver,
    Target,
    build_action_schedule,
    resolve_weights,
    run_schedule,
)


# --------------------------------------------------------------------------- #
# Pure schedule
# --------------------------------------------------------------------------- #


def test_schedule_is_deterministic_under_seed() -> None:
    a = build_action_schedule(seed=7, duration_minutes=60, actions_per_hour=30)
    b = build_action_schedule(seed=7, duration_minutes=60, actions_per_hour=30)
    assert a == b
    c = build_action_schedule(seed=8, duration_minutes=60, actions_per_hour=30)
    assert a != c


def test_schedule_count_and_ordering() -> None:
    schedule = build_action_schedule(
        seed=1, duration_minutes=120, actions_per_hour=30,
    )
    assert len(schedule) == 60  # 30/hr * 2h
    offsets = [a.offset_seconds for a in schedule]
    assert offsets == sorted(offsets)
    assert all(0.0 <= a.offset_seconds <= 120 * 60 for a in schedule)
    assert all(0.0 <= a.roll < 1.0 for a in schedule)
    assert {a.kind for a in schedule} <= {
        "chat", "freeze_toggle", "proactive_eval",
    }


def test_schedule_rejects_nonpositive() -> None:
    with pytest.raises(ValueError):
        build_action_schedule(seed=1, duration_minutes=0, actions_per_hour=30)
    with pytest.raises(ValueError):
        build_action_schedule(seed=1, duration_minutes=10, actions_per_hour=0)


# --------------------------------------------------------------------------- #
# Run loop
# --------------------------------------------------------------------------- #


class _RecordingDriver:
    def __init__(self) -> None:
        self.executed: list[ScheduledAction] = []

    async def execute(self, action: ScheduledAction) -> dict:
        self.executed.append(action)
        return {"ok": True}


@pytest.mark.asyncio
async def test_run_schedule_executes_all_when_not_stopped() -> None:
    driver = _RecordingDriver()
    schedule = tuple(
        ScheduledAction(offset_seconds=0.0, kind="chat", roll=0.1)
        for _ in range(3)
    )
    stop = asyncio.Event()
    executed = await run_schedule(
        driver, schedule, duration_seconds=100.0, stop_event=stop,
    )
    assert executed == 3
    assert len(driver.executed) == 3


@pytest.mark.asyncio
async def test_run_schedule_honours_preset_stop_event() -> None:
    driver = _RecordingDriver()
    schedule = (ScheduledAction(offset_seconds=0.0, kind="chat", roll=0.1),)
    stop = asyncio.Event()
    stop.set()
    executed = await run_schedule(
        driver, schedule, duration_seconds=100.0, stop_event=stop,
    )
    assert executed == 0


@pytest.mark.asyncio
async def test_run_schedule_honours_zero_duration() -> None:
    driver = _RecordingDriver()
    schedule = (ScheduledAction(offset_seconds=0.0, kind="chat", roll=0.1),)
    stop = asyncio.Event()
    executed = await run_schedule(
        driver, schedule, duration_seconds=0.0, stop_event=stop,
    )
    assert executed == 0


# --------------------------------------------------------------------------- #
# Target selection
# --------------------------------------------------------------------------- #


def test_target_selection_spans_pool() -> None:
    targets = [Target("op", f"c{i}") for i in range(4)]
    driver = SoakDriver(
        client=httpx.AsyncClient(),  # unused for select()
        targets=targets,
        journal=lambda _record: None,
    )
    assert driver.select(0.0).character_id == "c0"
    assert driver.select(0.99).character_id == "c3"
    assert driver.select(0.5).character_id == "c2"


# --------------------------------------------------------------------------- #
# Run health gate (M7)
# --------------------------------------------------------------------------- #


def _health(attempted, succeeded, enabled, *, per_kind=1, ratio=0.5):
    return RunHealth(
        attempted=attempted, succeeded=succeeded, enabled_kinds=tuple(enabled),
        min_success_per_kind=per_kind, min_success_ratio=ratio,
    )


def test_run_health_all_failures_is_unhealthy() -> None:
    # The core guarantee: an all-401/500 soak (attempted but zero success) must
    # NOT report healthy, so it can never exit 0.
    h = _health({"chat": 5}, {"chat": 0}, ["chat"])
    assert h.success_ratio == 0.0
    assert h.per_kind_ok is False
    assert h.healthy is False


def test_run_health_healthy_run() -> None:
    h = _health(
        {"chat": 5, "freeze_toggle": 3}, {"chat": 4, "freeze_toggle": 2},
        ["chat", "freeze_toggle"],
    )
    assert h.per_kind_ok is True
    assert h.success_ratio == 0.75
    assert h.healthy is True


def test_run_health_per_kind_floor_fails_one_dead_kind() -> None:
    # One enabled kind never succeeded → unhealthy even though the overall ratio
    # is high (a whole action path is broken).
    h = _health(
        {"chat": 10, "freeze_toggle": 4}, {"chat": 10, "freeze_toggle": 0},
        ["chat", "freeze_toggle"],
    )
    assert h.per_kind_ok is False
    assert h.healthy is False


def test_run_health_ratio_floor() -> None:
    # Each kind clears the per-kind floor (1 each) but the overall ratio is below
    # the threshold → unhealthy.
    h = _health(
        {"chat": 10, "freeze_toggle": 10}, {"chat": 1, "freeze_toggle": 1},
        ["chat", "freeze_toggle"],
    )
    assert h.per_kind_ok is True
    assert h.success_ratio == 0.1
    assert h.healthy is False


def test_run_health_nothing_attempted_is_unhealthy() -> None:
    assert _health({}, {}, ["chat"]).healthy is False


def test_resolve_weights_subscription_is_token_gated() -> None:
    assert "subscription_transition" not in resolve_weights(
        enable_subscription=False,
    )
    assert "subscription_transition" in resolve_weights(
        enable_subscription=True,
    )


# --------------------------------------------------------------------------- #
# Subscription-transition action (M7)
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_subscription_action_noop_without_token() -> None:
    async with httpx.AsyncClient(base_url="http://soak.test") as client:
        driver = SoakDriver(
            client=client, targets=[Target("op", "c1")],
            journal=lambda _r: None,  # no internal_token
        )
        result = await driver._do_subscription_transition(Target("op", "c1"))
    assert result["ok"] is False
    assert "no internal token" in result["error"]


@pytest.mark.asyncio
async def test_subscription_action_posts_to_internal_route_and_alternates() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200, json={"operators": 1, "frozen": 1, "failures": 0},
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://soak.test",
    ) as client:
        driver = SoakDriver(
            client=client, targets=[Target("op", "c1")],
            journal=lambda _r: None,
            internal_token="tok-123", tenant_id="tenant-A",
        )
        await driver.execute(ScheduledAction(
            offset_seconds=0.0, kind="subscription_transition", roll=0.0,
        ))
        await driver.execute(ScheduledAction(
            offset_seconds=0.0, kind="subscription_transition", roll=0.0,
        ))

    assert len(seen) == 2
    assert seen[0].url.path == "/api/internal/v1/cloud/subscription-freeze"
    assert seen[0].headers["authorization"] == "Bearer tok-123"
    assert json.loads(seen[0].content) == {
        "tenant_id": "tenant-A", "action": "freeze",
    }
    # The local view alternates freeze → unfreeze for the same tenant.
    assert json.loads(seen[1].content)["action"] == "unfreeze"
    # Counters tracked for the health gate.
    assert driver.attempted["subscription_transition"] == 2
    assert driver.succeeded["subscription_transition"] == 2


# --------------------------------------------------------------------------- #
# ASGI smoke (auth-off): one chat + one freeze toggle against the real app
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_chat_and_freeze_smoke_against_real_app() -> None:
    from kokoro_link.api.app import create_app
    from kokoro_link.api.dependencies import get_container
    from tests.unit._messaging_harness import (
        build_messaging_harness,
        build_service_container,
        create_character,
    )

    harness = build_messaging_harness()
    character = await create_character(harness, name="SoakSmoke")
    container = build_service_container(harness)
    # The admin freeze route reads container.character_repository; wire it to the
    # harness repo so the toggle hits a real (in-memory) character row.
    container.character_repository = harness.character_repository

    app = create_app()
    app.dependency_overrides[get_container] = lambda: container

    records: list[dict] = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://soak.test",
    ) as client:
        driver = SoakDriver(
            client=client,
            targets=[Target("default", character.id)],
            journal=records.append,
        )
        chat = await driver.execute(
            ScheduledAction(offset_seconds=0.0, kind="chat", roll=0.0),
        )
        freeze = await driver.execute(
            ScheduledAction(offset_seconds=0.0, kind="freeze_toggle", roll=0.0),
        )

    assert chat["result"]["ok"] is True, chat
    assert chat["result"]["status"] == 200
    assert freeze["result"]["ok"] is True, freeze
    assert freeze["result"]["action"] == "freeze"
    # The freeze actually landed on the character row.
    frozen = await harness.character_repository.get(character.id)
    assert frozen is not None and frozen.frozen is True
    assert len(records) == 2
