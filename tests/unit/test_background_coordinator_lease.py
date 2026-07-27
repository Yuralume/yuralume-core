"""Unit tests for the coordinator lease + cursor semantics (§3.2 / §4.2)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncio
import pytest

from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorCursor,
    InMemoryBackgroundCoordinatorLease,
)


pytestmark = pytest.mark.asyncio

NAME = "background-coordinator"
BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


async def test_acquire_free_lease_starts_epoch_one() -> None:
    lease = InMemoryBackgroundCoordinatorLease()
    assert await lease.current(NAME) is None
    epoch = await lease.acquire(NAME, "A", ttl_seconds=30, now=BASE)
    assert epoch == 1
    owner, current_epoch, until = await lease.current(NAME)
    assert owner == "A" and current_epoch == 1
    assert until == BASE + timedelta(seconds=30)


async def test_same_owner_reacquire_keeps_epoch_live_lease_blocks_others() -> None:
    lease = InMemoryBackgroundCoordinatorLease()
    await lease.acquire(NAME, "A", ttl_seconds=30, now=BASE)

    # Same owner re-acquire keeps the epoch and extends the lease.
    assert await lease.acquire(
        NAME, "A", ttl_seconds=30, now=BASE + timedelta(seconds=10),
    ) == 1
    # Different owner while lease live → denied.
    assert await lease.acquire(
        NAME, "B", ttl_seconds=30, now=BASE + timedelta(seconds=15),
    ) is None


async def test_same_owner_reacquire_after_expiry_bumps_epoch() -> None:
    # H2(b): a same-owner re-acquire over an EXPIRED lease is a NEW ownership
    # incarnation (the old one may be a partitioned zombie), so the epoch bumps
    # even though the owner id is unchanged — a live same-owner re-acquire keeps
    # the epoch (asserted separately above).
    lease = InMemoryBackgroundCoordinatorLease()
    assert await lease.acquire(NAME, "A", ttl_seconds=30, now=BASE) == 1

    # Lease expired (BASE+31 > BASE+30); same owner re-acquires → epoch 2.
    after = BASE + timedelta(seconds=31)
    assert await lease.acquire(NAME, "A", ttl_seconds=30, now=after) == 2
    owner, epoch, _until = await lease.current(NAME)
    assert owner == "A" and epoch == 2


async def test_renew_after_expiry_returns_none() -> None:
    # H2(a) Codex repro: an expired lease is NOT renewable even by its original
    # owner — it must be re-acquired (which bumps the epoch). A slow/partitioned
    # owner cannot silently keep the old epoch alive by renewing late.
    lease = InMemoryBackgroundCoordinatorLease()
    assert await lease.acquire(NAME, "A", ttl_seconds=30, now=BASE) == 1

    after = BASE + timedelta(seconds=31)  # lease_until (BASE+30) < after
    assert await lease.renew(NAME, "A", ttl_seconds=30, now=after) is None
    # A live renew still works (boundary: at exactly lease_until it is live).
    assert await lease.renew(
        NAME, "A", ttl_seconds=30, now=BASE + timedelta(seconds=30),
    ) == 1


async def test_takeover_after_expiry_bumps_epoch_monotonically() -> None:
    lease = InMemoryBackgroundCoordinatorLease()
    await lease.acquire(NAME, "A", ttl_seconds=30, now=BASE)

    # A's lease expires; B takes over → epoch 2.
    after = BASE + timedelta(seconds=31)
    assert await lease.acquire(NAME, "B", ttl_seconds=30, now=after) == 2

    # The partitioned old owner must stop: renew returns None.
    assert await lease.renew(NAME, "A", ttl_seconds=30, now=after) is None
    # The new owner renews and keeps its epoch.
    assert await lease.renew(NAME, "B", ttl_seconds=30, now=after) == 2


async def test_release_vacates_and_next_acquire_bumps_epoch() -> None:
    lease = InMemoryBackgroundCoordinatorLease()
    await lease.acquire(NAME, "A", ttl_seconds=30, now=BASE)

    assert not await lease.release(NAME, "B")  # not the owner
    assert await lease.release(NAME, "A")

    # After release, a fresh acquisition is an ownership change → epoch 2
    # (monotonic: the epoch is never reset even across release).
    assert await lease.acquire(
        NAME, "C", ttl_seconds=30, now=BASE + timedelta(seconds=5),
    ) == 2


async def test_cursor_round_trip() -> None:
    cursors = InMemoryBackgroundCoordinatorCursor()
    assert await cursors.get("due-scan") is None
    await cursors.set("due-scan", {"last_bucket": 42, "at": "2026-07-20"})
    assert await cursors.get("due-scan") == {
        "last_bucket": 42, "at": "2026-07-20",
    }
    await cursors.set("due-scan", {"last_bucket": 43})
    assert await cursors.get("due-scan") == {"last_bucket": 43}


async def test_advance_if_leader_is_atomic_monotonic_and_preserves_fields() -> None:
    lease = InMemoryBackgroundCoordinatorLease()
    cursors = InMemoryBackgroundCoordinatorCursor(lease)
    assert await lease.acquire(NAME, "A", ttl_seconds=30, now=BASE) == 1
    await cursors.set("due-scan", {"last_bucket": 10, "other": "kept"})

    assert await cursors.advance_if_leader(
        "due-scan", 20, NAME, "A", 1, BASE,
    )
    assert await cursors.advance_if_leader(
        "due-scan", 15, NAME, "A", 1, BASE,
    )
    assert await cursors.get("due-scan") == {"last_bucket": 20, "other": "kept"}

    later = BASE + timedelta(seconds=31)
    assert await lease.acquire(NAME, "B", ttl_seconds=30, now=later) == 2
    assert not await cursors.advance_if_leader(
        "due-scan", 30, NAME, "A", 1, later,
    )
    assert await cursors.get("due-scan") == {"last_bucket": 20, "other": "kept"}
    assert not await cursors.advance_if_leader(
        "due-scan", 30, NAME, "B", 1, later,
    )
    assert not await cursors.advance_if_leader(
        "due-scan", 30, NAME, "B", 2, later + timedelta(seconds=31),
    )


async def test_concurrent_advance_has_one_valid_winner_for_takeover() -> None:
    lease = InMemoryBackgroundCoordinatorLease()
    cursors = InMemoryBackgroundCoordinatorCursor(lease)
    assert await lease.acquire(NAME, "A", ttl_seconds=30, now=BASE) == 1
    await cursors.set("due-scan", {"last_bucket": 1})
    later = BASE + timedelta(seconds=31)
    assert await lease.acquire(NAME, "B", ttl_seconds=30, now=later) == 2

    results = await asyncio.gather(
        cursors.advance_if_leader("due-scan", 10, NAME, "A", 1, later),
        cursors.advance_if_leader("due-scan", 20, NAME, "B", 2, later),
    )
    assert results == [False, True]
    assert await cursors.get("due-scan") == {"last_bucket": 20}
