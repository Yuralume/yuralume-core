"""SSE streams close when the replica is drained (GD1-A).

``HOSTED_PROMPT_PACK_DISTRIBUTION_PLAN`` §7.3 GD0 records the trap this closes:
uvicorn counts an open SSE connection as in-flight work, so a replica that never
closes its streams spends the *entire* ``stop_grace_period`` babysitting idle
sockets and gets SIGKILLed anyway — the api role's share of the GD0 grace only
materialises once these generators return.

Two properties, and the second is the one that is easy to lose:

* the close is prompt. Waiting for the next 15s heartbeat would eat a tenth of
  the deploy's 180s budget per replica for no reason, so the generator is woken
  by the drain signal itself;
* the close is *gentle*. A plain ``return`` ends the stream cleanly and the
  browser's ``EventSource`` reconnects — onto a live replica, because the router
  removed this one before the drain request was sent. Raising instead would look
  like a broken connection and cost the client its reconnect backoff.

The unwired-container case is asserted too: no ``drain_state``, no new waiter,
no change to the stream a self-host process serves.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from kokoro_link.api.routes import events as events_route
from kokoro_link.application.services.drain_state import DrainState
from kokoro_link.application.services.feed_event_bus import FeedEventBus
from kokoro_link.application.services.proactive_event_bus import (
    ProactiveEvent,
    ProactiveEventBus,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 30, 12, 0, 0, tzinfo=timezone.utc)

#: Generous next to the 15s heartbeat and tight enough that passing it proves
#: the close came from the drain signal rather than from a tick.
_BOUND_SECONDS = 2.0


class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


def _container(drain_state: DrainState | None):
    container = SimpleNamespace(
        proactive_event_bus=ProactiveEventBus(),
        feed_event_bus=FeedEventBus(),
        realtime_outbox=None,
        realtime_rehydrator=None,
    )
    if drain_state is not None:
        container.drain_state = drain_state
    return container


async def _open(container):  # noqa: ANN001
    response = await events_route.events_stream(
        _FakeRequest(), container=container, current_user_id="u1",
        last_event_id=None,
    )
    gen = response.body_iterator
    assert await gen.__anext__() == ": connected\n\n"
    return gen


async def _settle() -> None:
    """Let the generator reach its ``asyncio.wait`` before the test acts."""
    for _ in range(5):
        await asyncio.sleep(0)


async def test_drain_closes_a_live_stream_within_a_bounded_time() -> None:
    drain = DrainState()
    gen = await _open(_container(drain))
    pending = asyncio.create_task(gen.__anext__())
    await _settle()
    assert not pending.done()  # parked on the 15s heartbeat wait

    drain.begin_drain()

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(pending, timeout=_BOUND_SECONDS)


async def test_a_stream_opened_after_the_drain_closes_immediately() -> None:
    # The router has already moved on, so this should not happen — but a
    # connection that slips through must not pin the replica for 15s either.
    drain = DrainState()
    drain.begin_drain()
    gen = await _open(_container(drain))

    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(gen.__anext__(), timeout=_BOUND_SECONDS)


async def test_events_still_flow_while_not_draining() -> None:
    """Regression fence: the extra waiter must not swallow real events."""
    drain = DrainState()
    container = _container(drain)
    gen = await _open(container)
    pending = asyncio.create_task(gen.__anext__())
    await _settle()

    await container.proactive_event_bus.publish(
        ProactiveEvent(
            character_id="char-1", conversation_id="conv-1", message="hi",
            created_at=BASE, unread_count=1,
        ),
    )
    frame = await asyncio.wait_for(pending, timeout=_BOUND_SECONDS)

    assert frame.startswith("event: proactive_message\n")
    assert drain.active_turns == 0
    await gen.aclose()


async def test_a_container_without_drain_state_is_unchanged() -> None:
    container = _container(None)
    gen = await _open(container)
    pending = asyncio.create_task(gen.__anext__())
    await _settle()

    # No drain wiring: nothing can end the stream but an event, a disconnect or
    # the heartbeat — exactly the self-host shape that shipped before GD1.
    assert not pending.done()
    await container.proactive_event_bus.publish(
        ProactiveEvent(
            character_id="char-1", conversation_id="conv-1", message="hi",
            created_at=BASE, unread_count=1,
        ),
    )
    assert (await asyncio.wait_for(pending, timeout=_BOUND_SECONDS)).startswith(
        "event: proactive_message\n",
    )
    await gen.aclose()
