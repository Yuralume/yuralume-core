"""PostgreSQL integration for the encounter pair lease (Phase 5 §6.1).

Proves the pair-atomic guarantees against the REAL SA adapter on the shared
``background_runtime_leases`` table (``encounter:<char_id>`` names), under
genuine concurrent transactions rather than the serialized in-memory lock:

* two workers racing ``acquire`` on the SAME pair → exactly one wins;
* crossing pairs ``(A,B)`` and ``(B,C)`` raced concurrently → no deadlock, the
  shared character ``B`` is claimed by exactly one side, all-or-nothing;
* ``(A,B)`` and ``(B,A)`` address the same canonical pair;
* an expired lease is taken over (new token) and the stale holder's heartbeat
  then fails;
* ``release`` frees both characters for immediate re-acquire.

All time is relative to a fixed base instant. Requires Docker (testcontainers);
skipped automatically when unavailable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.contracts.pair_lease import pair_lease_name
from kokoro_link.infrastructure.persistence.sa_pair_lease import SAPairLease

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


async def test_same_pair_race_exactly_one_wins(session_factory) -> None:
    lease = SAPairLease(session_factory)
    # Two workers hit the identical pair at the same instant.
    results = await asyncio.gather(
        lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE),
        lease.acquire("A", "B", "w2", ttl_seconds=100, now=BASE),
    )
    winners = [h for h in results if h is not None]
    assert len(winners) == 1
    assert (winners[0].char_a, winners[0].char_b) == ("A", "B")


async def test_crossing_pairs_no_deadlock_shared_char_single_holder(
    session_factory,
) -> None:
    lease = SAPairLease(session_factory)
    # (A,B) and (B,C) share B. Raced concurrently they must not deadlock; at
    # most one may hold B, and the loser must take neither row (all-or-nothing).
    ab, bc = await asyncio.wait_for(
        asyncio.gather(
            lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE),
            lease.acquire("B", "C", "w2", ttl_seconds=100, now=BASE),
        ),
        timeout=20,
    )
    winners = [h for h in (ab, bc) if h is not None]
    # Both pairs COULD both fail only if they deadlock-rolled-back; that cannot
    # happen with the single lock order — at least one wins B.
    assert len(winners) == 1
    winner = winners[0]
    # The character NOT shared by the winner is free; the shared B is held.
    if winner.char_a == "A":  # winner is (A,B) → C is free, B held
        free_char, held_pair_other = "C", "A"
    else:  # winner is (B,C) → A is free, B held
        free_char, held_pair_other = "A", "C"
    # The free character can still be paired with a fresh third character,
    # proving the losing acquire left NO residual row on it.
    residual_probe = await lease.acquire(
        free_char, "Z", "w3", ttl_seconds=100, now=BASE + timedelta(seconds=1),
    )
    assert residual_probe is not None
    # B remains held by the winner: a third pair touching B is denied.
    b_taker = await lease.acquire(
        "B", "Q", "w4", ttl_seconds=100, now=BASE + timedelta(seconds=2),
    )
    assert b_taker is None
    assert held_pair_other in (winner.char_a, winner.char_b)


async def test_reversed_args_are_same_canonical_pair(session_factory) -> None:
    lease = SAPairLease(session_factory)
    first = await lease.acquire("B", "A", "w1", ttl_seconds=100, now=BASE)
    assert first is not None
    assert (first.char_a, first.char_b) == ("A", "B")
    # (A, B) is the same canonical pair → denied while the (B, A) lease is live.
    second = await lease.acquire(
        "A", "B", "w2", ttl_seconds=100, now=BASE + timedelta(seconds=1),
    )
    assert second is None


async def test_expired_takeover_and_stale_heartbeat_fails(
    session_factory,
) -> None:
    lease = SAPairLease(session_factory)
    handle = await lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE)
    assert handle is not None
    # Heartbeat while live renews.
    assert await lease.heartbeat(
        handle, now=BASE + timedelta(seconds=50),
    ) is True

    after = BASE + timedelta(seconds=200)  # both original TTL + renewal lapsed
    taken = await lease.acquire("A", "B", "w2", ttl_seconds=100, now=after)
    assert taken is not None
    assert taken.token != handle.token
    # The stale holder's heartbeat now fails — the pair-job abort cue.
    assert await lease.heartbeat(
        handle, now=after + timedelta(seconds=1),
    ) is False
    # The new holder still heartbeats fine.
    assert await lease.heartbeat(
        taken, now=after + timedelta(seconds=2),
    ) is True


async def test_expired_holder_heartbeat_fails_without_takeover(
    session_factory,
) -> None:
    lease = SAPairLease(session_factory)
    handle = await lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE)
    assert handle is not None
    # Expired, nobody took over: still not renewable by the former holder.
    assert await lease.heartbeat(
        handle, now=BASE + timedelta(seconds=101),
    ) is False


async def test_release_frees_both_characters(session_factory) -> None:
    lease = SAPairLease(session_factory)
    handle = await lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE)
    assert handle is not None
    await lease.release(handle)
    # Both characters re-acquirable immediately (before the TTL would lapse).
    again = await lease.acquire(
        "A", "B", "w2", ttl_seconds=100, now=BASE + timedelta(seconds=5),
    )
    assert again is not None
    # Cross-check the low row is genuinely owned by the new token.
    assert again.names[0] == pair_lease_name("A")


async def test_release_under_superseded_token_is_noop(session_factory) -> None:
    lease = SAPairLease(session_factory)
    handle = await lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE)
    assert handle is not None
    after = BASE + timedelta(seconds=101)
    taken = await lease.acquire("A", "B", "w2", ttl_seconds=100, now=after)
    assert taken is not None
    # The stale holder releasing must not disturb the new owner's rows.
    await lease.release(handle)
    assert await lease.heartbeat(
        taken, now=after + timedelta(seconds=1),
    ) is True
