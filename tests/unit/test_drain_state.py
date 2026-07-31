"""The drain switch itself (GD1-A).

Everything downstream — the endpoint, the metrics gauge, the chat gate, the SSE
close — trusts this object's two numbers, and a deploy trusts them enough to send
SIGKILL. So the invariants that make ``active_turns == 0`` mean "nothing will be
lost" are pinned here rather than inferred from the callers.
"""

from __future__ import annotations

import asyncio

import pytest

from kokoro_link.application.services.drain_state import (
    SERVER_DRAINING_CODE,
    ActiveTurn,
    DrainState,
    ServerDrainingError,
)


def test_a_fresh_state_accepts_everything() -> None:
    state = DrainState()

    assert state.draining is False
    assert state.active_turns == 0
    state.ensure_accepting()  # must not raise


def test_drain_is_one_way_and_idempotent() -> None:
    state = DrainState()

    state.begin_drain()
    state.begin_drain()

    assert state.draining is True
    with pytest.raises(ServerDrainingError) as caught:
        state.ensure_accepting()
    assert caught.value.code == SERVER_DRAINING_CODE
    # There is deliberately no un-drain: a replica that flip-flopped would be a
    # replica whose active_turns reading nobody could act on. Restart resets it.
    assert not hasattr(state, "stop_draining")


def test_turns_are_counted_and_release_is_idempotent() -> None:
    state = DrainState()

    first = state.try_enter_turn()
    second = state.try_enter_turn()
    assert state.active_turns == 2

    first.release()
    first.release()  # the streaming path really does release twice
    assert state.active_turns == 1

    second.release()
    assert state.active_turns == 0


def test_the_counter_never_goes_negative() -> None:
    # A negative counter would let one double-release mask a genuinely running
    # turn, which is the one lie this gauge must not be able to tell.
    state = DrainState()
    stray = ActiveTurn(state)

    stray.release()

    assert state.active_turns == 0


def test_turns_are_counted_while_draining() -> None:
    """Drain refuses *new* turns; it does not disown the ones already running."""
    state = DrainState()
    turn = state.try_enter_turn()

    state.begin_drain()

    assert state.active_turns == 1
    turn.release()
    assert state.active_turns == 0


def test_admission_refuses_and_counts_nothing_while_draining() -> None:
    state = DrainState()
    state.begin_drain()

    with pytest.raises(ServerDrainingError) as caught:
        state.try_enter_turn()

    assert caught.value.code == SERVER_DRAINING_CODE
    # A refused turn must leave the gauge alone, or the deploy would wait out
    # its whole budget for turns that were never admitted.
    assert state.active_turns == 0


def test_admission_is_one_indivisible_step() -> None:
    """The check and the count are the same call — there is no seam.

    This is the C2 bug in miniature: while these were two members
    (``ensure_accepting`` then ``enter_turn``) a caller could pass the check,
    await something, and only then be counted — and everything awaited in
    between was invisible to a deploy polling ``active_turns`` for zero. The
    property that closes it is structural, so it is asserted structurally: the
    admission entry point takes no arguments, returns the handle, and there is
    no public member that increments without checking.
    """
    state = DrainState()

    handle = state.try_enter_turn()
    assert state.active_turns == 1

    assert not hasattr(state, "enter_turn")
    handle.release()
    assert state.active_turns == 0


def test_an_unwired_handle_counts_nothing() -> None:
    inert = ActiveTurn()

    with inert as entered:
        assert entered is inert
    inert.release()  # no state to touch, no crash


def test_the_context_manager_releases_on_failure() -> None:
    state = DrainState()

    with pytest.raises(RuntimeError):
        with state.try_enter_turn():
            raise RuntimeError("turn exploded")

    assert state.active_turns == 0


@pytest.mark.asyncio
async def test_waiting_wakes_on_drain() -> None:
    state = DrainState()
    waiter = asyncio.create_task(state.wait_draining())
    await asyncio.sleep(0)
    assert not waiter.done()

    state.begin_drain()

    await asyncio.wait_for(waiter, timeout=2)


@pytest.mark.asyncio
async def test_waiting_after_drain_returns_immediately() -> None:
    state = DrainState()
    state.begin_drain()

    await asyncio.wait_for(state.wait_draining(), timeout=2)
