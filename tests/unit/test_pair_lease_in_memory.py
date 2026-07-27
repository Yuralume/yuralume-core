"""Unit coverage for the in-memory encounter pair lease (Phase 5 §6.1).

Pins the pure-logic contract of :class:`InMemoryPairLease` — the parity backend
for the SA adapter: canonical ordering, atomic all-or-nothing acquire, the
single-character-single-pair invariant, TTL takeover + stale heartbeat, and
release-then-reacquire. All time is relative to a fixed base instant per the
repo convention — never wall-clock.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.contracts.pair_lease import (
    PairLeasePort,
    canonical_pair,
    pair_lease_name,
)
from kokoro_link.infrastructure.repositories.in_memory_pair_lease import (
    InMemoryPairLease,
)


BASE = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)

_asyncio = pytest.mark.asyncio


def _lease() -> InMemoryPairLease:
    return InMemoryPairLease()


class TestCanonicalHelpers:
    def test_canonical_pair_sorts(self) -> None:
        assert canonical_pair("b", "a") == ("a", "b")
        assert canonical_pair("a", "b") == ("a", "b")

    def test_canonical_pair_rejects_self_and_empty(self) -> None:
        with pytest.raises(ValueError):
            canonical_pair("a", "a")
        with pytest.raises(ValueError):
            canonical_pair("", "b")
        with pytest.raises(ValueError):
            canonical_pair("  ", "b")

    def test_name_namespace(self) -> None:
        assert pair_lease_name("c1") == "encounter:c1"

    def test_structural_port(self) -> None:
        assert isinstance(_lease(), PairLeasePort)


@_asyncio
class TestAcquire:
    async def test_acquire_returns_canonical_handle(self) -> None:
        lease = _lease()
        # (B, A) canonicalises to (A, B) — same logical pair.
        handle = await lease.acquire("B", "A", "w1", ttl_seconds=100, now=BASE)
        assert handle is not None
        assert (handle.char_a, handle.char_b) == ("A", "B")
        assert handle.owner == "w1"
        assert handle.names == ("encounter:A", "encounter:B")
        assert handle.lease_until == BASE + timedelta(seconds=100)

    async def test_same_pair_second_acquire_denied_while_live(self) -> None:
        lease = _lease()
        first = await lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE)
        assert first is not None
        # (B, A) is the SAME canonical pair → denied while the first is live,
        # even for a different worker.
        second = await lease.acquire(
            "B", "A", "w2", ttl_seconds=100, now=BASE + timedelta(seconds=1),
        )
        assert second is None

    async def test_disjoint_pairs_independent(self) -> None:
        lease = _lease()
        ab = await lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE)
        cd = await lease.acquire("C", "D", "w2", ttl_seconds=100, now=BASE)
        assert ab is not None and cd is not None


@_asyncio
class TestSingleCharacterSinglePair:
    async def test_shared_character_blocks_second_pair_all_or_nothing(
        self,
    ) -> None:
        lease = _lease()
        ab = await lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE)
        assert ab is not None
        # (B, C) shares B → must fail as a whole; C must NOT be left held.
        bc = await lease.acquire(
            "B", "C", "w2", ttl_seconds=100, now=BASE + timedelta(seconds=1),
        )
        assert bc is None
        # Proof of no residual: an unrelated pair (C, D) can still take C.
        cd = await lease.acquire(
            "C", "D", "w3", ttl_seconds=100, now=BASE + timedelta(seconds=2),
        )
        assert cd is not None

    async def test_same_worker_cannot_hold_character_in_two_pairs(self) -> None:
        lease = _lease()
        ab = await lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE)
        assert ab is not None
        # Same worker, different pair sharing B → still denied (the guard is on
        # the character, not the worker).
        bc = await lease.acquire(
            "B", "C", "w1", ttl_seconds=100, now=BASE + timedelta(seconds=1),
        )
        assert bc is None


@_asyncio
class TestHeartbeatAndTtl:
    async def test_heartbeat_renews_while_owned(self) -> None:
        lease = _lease()
        handle = await lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE)
        assert handle is not None
        ok = await lease.heartbeat(handle, now=BASE + timedelta(seconds=50))
        assert ok is True
        # Renewal pushed expiry out: a would-be taker at the ORIGINAL expiry is
        # still denied.
        taker = await lease.acquire(
            "A", "B", "w2", ttl_seconds=100, now=BASE + timedelta(seconds=120),
        )
        assert taker is None

    async def test_expired_lease_taken_over_and_stale_heartbeat_fails(
        self,
    ) -> None:
        lease = _lease()
        handle = await lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE)
        assert handle is not None
        after = BASE + timedelta(seconds=101)
        # After the TTL lapses another pair-holder takes over.
        taken = await lease.acquire("A", "B", "w2", ttl_seconds=100, now=after)
        assert taken is not None
        assert taken.token != handle.token
        # The stale holder's heartbeat now fails — its abort cue.
        assert await lease.heartbeat(
            handle, now=after + timedelta(seconds=1),
        ) is False

    async def test_expired_holder_heartbeat_fails_even_without_takeover(
        self,
    ) -> None:
        lease = _lease()
        handle = await lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE)
        assert handle is not None
        # Nobody took over, but the lease expired: it is not renewable by its
        # former holder — must be re-acquired.
        assert await lease.heartbeat(
            handle, now=BASE + timedelta(seconds=101),
        ) is False


@_asyncio
class TestRelease:
    async def test_release_allows_reacquire(self) -> None:
        lease = _lease()
        handle = await lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE)
        assert handle is not None
        await lease.release(handle)
        # Both characters are free again immediately (before TTL).
        again = await lease.acquire(
            "A", "B", "w2", ttl_seconds=100, now=BASE + timedelta(seconds=5),
        )
        assert again is not None

    async def test_release_under_superseded_token_is_noop(self) -> None:
        lease = _lease()
        handle = await lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE)
        assert handle is not None
        after = BASE + timedelta(seconds=101)
        taken = await lease.acquire("A", "B", "w2", ttl_seconds=100, now=after)
        assert taken is not None
        # The stale holder releasing must NOT disturb the new owner's rows.
        await lease.release(handle)
        assert await lease.heartbeat(
            taken, now=after + timedelta(seconds=1),
        ) is True

    async def test_heartbeat_after_release_fails(self) -> None:
        lease = _lease()
        handle = await lease.acquire("A", "B", "w1", ttl_seconds=100, now=BASE)
        assert handle is not None
        await lease.release(handle)
        assert await lease.heartbeat(
            handle, now=BASE + timedelta(seconds=1),
        ) is False
