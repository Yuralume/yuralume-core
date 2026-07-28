import asyncio

import pytest

from kokoro_link.application.services.cloud_billing_context import (
    BILLING_IDEMPOTENCY_HEADER,
    billing_idempotency_headers,
    cloud_billing_operation,
    cloud_billing_scope,
)


def _key(payload: dict[str, object]) -> str | None:
    return billing_idempotency_headers(
        capability="llm",
        feature_key="chat",
        payload=payload,
    ).get(BILLING_IDEMPOTENCY_HEADER)


def test_no_billing_key_outside_durable_scope() -> None:
    assert _key({"model": "test", "messages": []}) is None


def test_replay_produces_same_keys_for_same_logical_sequence() -> None:
    payload = {"model": "test", "messages": [{"role": "user", "content": "hi"}]}
    with cloud_billing_scope("external-chat:turn-1"):
        first_run = [_key(payload), _key(payload)]
    with cloud_billing_scope("external-chat:turn-1"):
        replay = [_key(payload), _key(payload)]

    assert replay == first_run
    assert first_run[0] != first_run[1]


def test_scope_feature_and_occurrence_are_part_of_the_key_but_payload_is_not() -> None:
    with cloud_billing_scope("external-chat:turn-1"):
        first = _key({"model": "a"})
        second_occurrence = _key({"model": "b"})
        different_feature = billing_idempotency_headers(
            capability="llm",
            feature_key="register_profile",
            payload={"model": "a"},
        )[BILLING_IDEMPOTENCY_HEADER]
    with cloud_billing_scope("external-chat:turn-2"):
        different_scope = _key({"model": "a"})

    assert len({first, second_occurrence, different_feature, different_scope}) == 4


def test_retry_across_wall_clock_prompt_drift_keeps_first_operation_key() -> None:
    with cloud_billing_scope("external-chat:turn-minute-drift"):
        before = _key({"prompt": "current time: 14:58"})
    with cloud_billing_scope("external-chat:turn-minute-drift"):
        after = _key({"prompt": "current time: 14:59", "tool_result": "new"})

    assert after == before


@pytest.mark.asyncio
async def test_child_task_and_parent_calls_never_collide() -> None:
    with cloud_billing_scope("external-chat:turn-child"):
        first = _key({"prompt": "first"})

        async def child_call() -> str | None:
            return _key({"prompt": "child"})

        child_second = await asyncio.create_task(child_call())
        parent_second = _key({"prompt": "parent"})

    assert first != child_second
    assert len({first, child_second, parent_second}) == 3


@pytest.mark.asyncio
async def test_parallel_semantic_operations_are_stable_when_schedule_reverses() -> None:
    async def operation(label: str, *, delay: float) -> str | None:
        await asyncio.sleep(delay)
        with cloud_billing_operation(label):
            return _key({"prompt": f"dynamic {label}"})

    with cloud_billing_scope("external-chat:turn-parallel"):
        first_user, first_assistant = await asyncio.gather(
            operation("safe-summary:user", delay=0.01),
            operation("safe-summary:assistant", delay=0),
        )
    with cloud_billing_scope("external-chat:turn-parallel"):
        replay_user, replay_assistant = await asyncio.gather(
            operation("safe-summary:user", delay=0),
            operation("safe-summary:assistant", delay=0.01),
        )

    assert first_user == replay_user
    assert first_assistant == replay_assistant
    assert first_user != first_assistant
