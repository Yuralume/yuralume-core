from __future__ import annotations

import asyncio

import pytest

from kokoro_link.contracts.generation_trigger import (
    GenerationTrigger,
    current_generation_trigger,
    generation_trigger_header_value,
    generation_trigger_scope,
)


def test_generation_trigger_defaults_to_user_action_and_restores_nested_scope() -> None:
    assert current_generation_trigger() is GenerationTrigger.USER_ACTION
    assert generation_trigger_header_value() == "v1;source=user_action"

    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
        assert generation_trigger_header_value() == "v1;source=background"
        with generation_trigger_scope(GenerationTrigger.OFFLINE):
            assert generation_trigger_header_value() == "v1;source=offline"
        assert generation_trigger_header_value() == "v1;source=background"

    assert generation_trigger_header_value() == "v1;source=user_action"


@pytest.mark.asyncio
async def test_generation_trigger_propagates_to_child_async_task() -> None:
    async def read_header() -> str:
        await asyncio.sleep(0)
        return generation_trigger_header_value()

    with generation_trigger_scope(GenerationTrigger.BACKGROUND):
        task = asyncio.create_task(read_header())
    assert await task == "v1;source=background"
    assert generation_trigger_header_value() == "v1;source=user_action"
