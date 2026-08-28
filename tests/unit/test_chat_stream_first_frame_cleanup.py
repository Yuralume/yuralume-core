"""The SSE route's first frame must not be a hole in the turn's lifecycle (C3).

The streaming chat route opens by yielding the ``conversation_id`` frame so the
frontend can bind the conversation before a single token arrives. That yield
used to sit *above* the ``try``/``finally`` that ends the turn — which meant a
client that went away while exactly that frame was in flight closed the
generator at a suspension point no cleanup covered:

* the conversation lease stayed claimed until its 180s TTL, so the player's own
  retry came back ``conversation_busy``;
* the action charge stayed reserved until the stale-reservation sweeper ran;
* and ``active_turns`` — an in-process integer with no TTL and no sweeper —
  stayed above zero **for the life of the process**, so every subsequent drain
  spent its whole 180s budget waiting for a turn that ended long ago before
  killing the replica anyway. GD's central promise, silently voided by a
  disconnect at the one moment disconnects cluster: hitting send and
  immediately navigating away.

What the frame-one disconnect *means* changed with detached completion: it is
no longer an abandoned turn to be released, it is an audience leaving a
performance that carries on. So the assertions here moved from "everything is
released immediately" to "the turn still lands, and it lands exactly once" —
the hole the original bug opened is still closed, just at the other end.

The route is driven directly rather than through ``TestClient`` because the
event under test is "the consumer stops consuming at frame one", which is
``aclose()`` on the response's body iterator — precisely what Starlette does to
an abandoned stream, and not something an HTTP round trip can express.

Every test here asserts ``relay.detached`` as well as the outcome, and that is
the load-bearing half. The outcomes — message persisted, charge settled, drain
slot back to zero — are what a turn produces whether or not the ``finally``
ever ran: these replies are far shorter than the frame buffer, so the turn task
runs to completion on its own with nobody reading it. Moving the first ``yield``
back above the ``try``, or emptying ``detach()`` out entirely, leaves all three
of them green. ``detached`` is the only assertion those mutations turn red, and
it can only have been set by the route's ``finally``: the relay's own
stalled-transport timeout is three minutes away and this test finishes in
milliseconds.
"""

from __future__ import annotations

import json

import pytest

from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.chat_stream_relay import (
    wait_for_pending_turn_completions,
)

from tests.unit.chat_stream_route_harness import USER_ID, StreamRouteFixture

pytestmark = pytest.mark.asyncio


async def test_a_disconnect_on_the_first_frame_still_completes_the_turn() -> None:
    fixture = StreamRouteFixture()
    character_id = await fixture.seed_character()

    response = await fixture.open_stream(character_id, "第一幀就跑掉")
    frames = response.body_iterator
    first = await frames.__anext__()

    # Frame one is the conversation binding, and the turn is fully in flight:
    # charge opened, conversation claimed, drain slot taken.
    assert json.loads(first.removeprefix("data: ").strip())["conversation_id"]
    assert fixture.billing.begins == 1
    assert fixture.drain.active_turns == 1

    # The client goes away right here. Starlette closes the body iterator, which
    # throws ``GeneratorExit`` into the generator at that first yield.
    await frames.aclose()

    # The ``finally`` ran *at that yield* — this is the assertion the file
    # exists for. It is checked before the turn is awaited because after the
    # turn has landed nothing else can distinguish "the disconnect was handled"
    # from "the turn happened to finish first".
    relay = fixture.relays[-1]
    assert relay.detached is True

    await wait_for_pending_turn_completions()

    # The player paid for a message, so a message exists — the whole point of
    # detaching instead of cancelling. And the turn is over exactly once.
    assert fixture.billing.settles == 1
    assert fixture.drain.active_turns == 0
    assert len(await fixture.assistant_messages(character_id)) == 1


async def test_the_conversation_is_free_again_once_the_turn_lands() -> None:
    """The lease half of the same leak, asserted where a player would feel it.

    A stranded lease is not visible as a counter; it is visible as the player's
    very next message coming back ``conversation_busy``. The turn now holds the
    conversation until the reply is persisted rather than until the socket
    closes — the drain contract, applied to the player-visible surface — so the
    assertion is that the retry goes through afterwards, not instantly.
    """
    fixture = StreamRouteFixture()
    character_id = await fixture.seed_character()

    response = await fixture.open_stream(character_id, "送出後立刻離開")
    frames = response.body_iterator
    first = await frames.__anext__()
    conversation_id = json.loads(
        first.removeprefix("data: ").strip(),
    )["conversation_id"]
    await frames.aclose()
    assert fixture.relays[-1].detached is True
    await wait_for_pending_turn_completions()

    retry = await fixture.chat_service.send_message(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="我回來了",
            provider_id="fake",
        ),
        current_user_id=USER_ID,
    )

    assert retry.assistant_message is not None
    assert fixture.drain.active_turns == 0


async def test_a_fully_consumed_stream_still_settles_normally() -> None:
    """The regression guard: moving the frame must not change the happy path."""
    fixture = StreamRouteFixture()
    character_id = await fixture.seed_character()

    response = await fixture.open_stream(character_id, "好好講完")
    frames = [chunk async for chunk in response.body_iterator]

    assert frames[0].startswith("data: ")
    assert "conversation_id" in frames[0]
    assert frames[-1] == "data: [DONE]\n\n"
    assert any('"done": true' in f for f in frames)
    assert fixture.billing.settles == 1
    assert fixture.billing.releases == 1  # the finalizer's idempotent no-op
    assert fixture.drain.active_turns == 0
