"""LLM request priority gate (HUMANIZATION_ROADMAP §4.5).

Owner decision (2026-05-21): we don't write a full LM Studio queue
adapter — the local model already serialises requests on the GPU. What
we *can* do is **order our own callers** so chat-path requests don't
get stuck behind a dream-pass batch when both fire on the same tick.

Design:

* :class:`LLMRequestPriority` — small enum of caller classes.
* :class:`LLMSerialisationGate` — async context manager. The gate
  holds a single FIFO + priority slot; callers ``async with
  gate.acquire(priority)`` to enter the critical section. A worker
  task feeds the priority queue so when chat and dream both want the
  GPU at once, chat goes first.

Why not wrap every ``ChatModelPort.generate()`` call: the contract
surface is hot and rewriting every caller would balloon the diff.
The gate stays opt-in — callers that benefit (dream pass, embedding
sync, proactive dispatcher) wrap their existing LLM call inside
``async with gate.acquire(LLMRequestPriority.DREAM):`` and skip the
gate for fire-and-forget paths.

LLM-first 紅線reminder: the gate is **infrastructure**, not a feature
toggle. It never reads character state, prompt content, or any other
semantic signal — it only orders requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import heapq
import itertools
import logging
from dataclasses import dataclass, field
from typing import AsyncIterator

_LOGGER = logging.getLogger(__name__)


class LLMRequestPriority(enum.IntEnum):
    """Lower number = higher priority. Same as Unix nice values."""

    CHAT = 0
    """Direct user-facing reply path. Must never wait behind background jobs."""
    TTS = 1
    """Speech synthesis, usually blocks UI playback."""
    PROACTIVE = 2
    """Proactive decider / intention judge. Background but user-perceptible."""
    EMBEDDING = 3
    """Embedding sync. Pure background, no user wait."""
    DREAM = 4
    """Dream-time consolidation / reflection / extraction jobs."""


@dataclass(order=True)
class _WaitTicket:
    priority: int
    sequence: int
    waiter: asyncio.Future = field(compare=False)


class LLMSerialisationGate:
    """Single-slot priority gate.

    ``concurrency=1`` is the realistic default: a local LM Studio
    instance hits one GPU and serialises anyway, so the gate's value
    is **ordering**, not parallelism. Raise concurrency only when you
    have multiple model backends that genuinely run in parallel.
    """

    def __init__(self, *, concurrency: int = 1) -> None:
        if concurrency < 1:
            raise ValueError("LLMSerialisationGate concurrency must be >= 1")
        self._semaphore = asyncio.Semaphore(concurrency)
        self._heap: list[_WaitTicket] = []
        self._counter = itertools.count()
        self._lock = asyncio.Lock()
        self._dispatcher_running = False

    @contextlib.asynccontextmanager
    async def acquire(
        self, priority: LLMRequestPriority,
    ) -> AsyncIterator[None]:
        """Enter the critical section honouring the given priority.

        Callers that don't care about ordering should NOT use this gate
        — the cost is one extra context switch per LLM call, and the
        chat path already runs without it for the lowest possible
        latency.
        """
        ticket = _WaitTicket(
            priority=int(priority),
            sequence=next(self._counter),
            waiter=asyncio.get_running_loop().create_future(),
        )
        async with self._lock:
            heapq.heappush(self._heap, ticket)
            if not self._dispatcher_running:
                self._dispatcher_running = True
                asyncio.create_task(self._dispatch())
        try:
            await ticket.waiter
            yield
        finally:
            self._semaphore.release()

    async def _dispatch(self) -> None:
        """Buy a slot, then pop the highest-priority ticket.

        Order matters — if we popped *before* the semaphore acquired
        we'd commit to a ticket while other higher-priority callers
        are still queueing up. By acquiring the slot first we always
        pick from the *freshest* heap snapshot.

        Runs as a long-lived background task while the queue is
        non-empty; exits when the heap drains and a future ``acquire``
        spins it back up.
        """
        try:
            while True:
                await self._semaphore.acquire()
                async with self._lock:
                    if not self._heap:
                        self._dispatcher_running = False
                        # Hand the slot back since no one is going to
                        # consume it. The next acquire() restarts us.
                        self._semaphore.release()
                        return
                    ticket = heapq.heappop(self._heap)
                if not ticket.waiter.done():
                    ticket.waiter.set_result(None)
        except Exception:
            _LOGGER.exception("LLMSerialisationGate dispatcher crashed")
            async with self._lock:
                self._dispatcher_running = False
