"""EncounterPairLeaseSession — acquire / heartbeat-loss / release parity (§6.1)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.encounter_pair_lease import (
    EncounterPairLeaseLost,
    EncounterPairLeaseSession,
)
from kokoro_link.contracts.clock import ClockPort
from kokoro_link.infrastructure.repositories.in_memory_pair_lease import (
    InMemoryPairLease,
)

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


class _FrozenClock(ClockPort):
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


async def test_null_lease_is_always_acquired_no_op() -> None:
    async with EncounterPairLeaseSession(None, "a", "b", owner="w") as session:
        assert session.acquired is True
        session.raise_if_lost()  # never lost


async def test_acquire_release_round_trip() -> None:
    lease = InMemoryPairLease()
    async with EncounterPairLeaseSession(
        lease, "b", "a", owner="w", ttl_seconds=60, clock=_FrozenClock(BASE),
        heartbeat_interval_seconds=1000.0,  # never fires in the test window
    ) as session:
        assert session.acquired is True
        # Both underlying rows are now held → a crossing acquire fails.
        assert await lease.acquire("a", "c", "w2", ttl_seconds=60, now=BASE) is None
    # After exit the rows are released → the crossing acquire succeeds.
    assert await lease.acquire("a", "c", "w2", ttl_seconds=60, now=BASE) is not None


async def test_contended_pair_is_not_acquired() -> None:
    lease = InMemoryPairLease()
    # A different holder already owns character "a".
    assert await lease.acquire("a", "z", "other", ttl_seconds=300, now=BASE) is not None
    async with EncounterPairLeaseSession(
        lease, "a", "b", owner="w", ttl_seconds=60, clock=_FrozenClock(BASE),
    ) as session:
        assert session.acquired is False


class _LosesLeaseAfterAcquire:
    """Pair lease whose heartbeat reports the lease lost (takeover / expiry)."""

    def __init__(self) -> None:
        self.released: list[object] = []

    async def acquire(self, char_a, char_b, owner, *, ttl_seconds, now):  # noqa: ANN001
        from kokoro_link.contracts.pair_lease import PairLeaseHandle, canonical_pair

        low, high = canonical_pair(char_a, char_b)
        return PairLeaseHandle(
            char_a=low, char_b=high, owner=owner, token="t",
            ttl_seconds=ttl_seconds, lease_until=now,
        )

    async def heartbeat(self, handle, *, now):  # noqa: ANN001
        return False  # lost: taken over / expired

    async def release(self, handle) -> None:  # noqa: ANN001
        self.released.append(handle)


async def test_heartbeat_loss_trips_lost_and_raises() -> None:
    lease = _LosesLeaseAfterAcquire()
    session = EncounterPairLeaseSession(
        lease, "a", "b", owner="w", ttl_seconds=60, clock=_FrozenClock(BASE),
        heartbeat_interval_seconds=0.01,
    )
    async with session:
        assert session.acquired is True
        for _ in range(200):
            if session.lost:
                break
            await asyncio.sleep(0.005)
        assert session.lost is True
        with pytest.raises(EncounterPairLeaseLost):
            session.raise_if_lost()
    # A lost lease is NOT released (it belongs to the reclaiming holder now).
    assert lease.released == []
