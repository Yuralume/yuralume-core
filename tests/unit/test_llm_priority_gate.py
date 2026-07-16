"""Unit tests for :class:`LLMSerialisationGate` (HUMANIZATION_ROADMAP §4.5)."""

from __future__ import annotations

import asyncio

import pytest

from kokoro_link.infrastructure.llm.priority_gate import (
    LLMRequestPriority,
    LLMSerialisationGate,
)


@pytest.mark.asyncio
async def test_higher_priority_runs_first_even_when_queued_later() -> None:
    """With concurrency=1, queue a long-running DREAM holder, then push
    PROACTIVE and CHAT while it's still busy — when the holder releases,
    CHAT must come out before PROACTIVE."""
    gate = LLMSerialisationGate(concurrency=1)
    order: list[str] = []
    holder_release = asyncio.Event()

    async def holder():
        async with gate.acquire(LLMRequestPriority.DREAM):
            order.append("dream")
            await holder_release.wait()  # block until both waiters queue

    async def queued_worker(priority: LLMRequestPriority, label: str):
        async with gate.acquire(priority):
            order.append(label)

    holder_task = asyncio.create_task(holder())
    # Wait one event-loop tick so the holder definitely grabs the slot
    # before either contender queues up.
    await asyncio.sleep(0.005)
    proactive_task = asyncio.create_task(
        queued_worker(LLMRequestPriority.PROACTIVE, "proactive"),
    )
    chat_task = asyncio.create_task(
        queued_worker(LLMRequestPriority.CHAT, "chat"),
    )
    await asyncio.sleep(0.01)  # let both register with the heap
    holder_release.set()
    await asyncio.gather(holder_task, proactive_task, chat_task)

    assert order[0] == "dream"
    assert order.index("chat") < order.index("proactive")


@pytest.mark.asyncio
async def test_concurrent_acquires_serialise_with_one_slot() -> None:
    gate = LLMSerialisationGate(concurrency=1)
    active = 0
    max_active = 0

    async def worker():
        nonlocal active, max_active
        async with gate.acquire(LLMRequestPriority.CHAT):
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.005)
            active -= 1

    await asyncio.gather(*[worker() for _ in range(8)])
    assert max_active == 1


@pytest.mark.asyncio
async def test_invalid_concurrency_rejected() -> None:
    with pytest.raises(ValueError):
        LLMSerialisationGate(concurrency=0)


@pytest.mark.asyncio
async def test_priority_enum_ordering() -> None:
    """Priority values must remain in chat < tts < proactive < embedding < dream
    order — downstream wires assume this ranking for nice-style routing."""
    assert LLMRequestPriority.CHAT < LLMRequestPriority.TTS
    assert LLMRequestPriority.TTS < LLMRequestPriority.PROACTIVE
    assert LLMRequestPriority.PROACTIVE < LLMRequestPriority.EMBEDDING
    assert LLMRequestPriority.EMBEDDING < LLMRequestPriority.DREAM
