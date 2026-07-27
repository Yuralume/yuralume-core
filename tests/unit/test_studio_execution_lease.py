"""Unit coverage for the per-target Creator Studio execution lease.

HOSTED_CORE_SCALING_ARCHITECTURE_PLAN §13 Phase 4 前置 1.

Exercises the lease + session in isolation against the in-process
``InMemoryBackgroundCoordinatorLease`` backend (same TTL + monotonic-epoch
contract as the SA adapter): acquire / heartbeat / release, epoch preemption
after a takeover, the null (lease-less) session, and cooperative abort when a
heartbeat reports the lease lost. All time is relative to a fixed base instant —
never wall-clock — per the repo test convention.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.studio_execution_lease import (
    StudioExecutionLease,
    StudioLeaseLost,
    StudioLeaseSession,
)
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
)


BASE = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


def _lease(owner: str, *, ttl: int = 100) -> tuple[StudioExecutionLease, InMemoryBackgroundCoordinatorLease]:
    backend = InMemoryBackgroundCoordinatorLease()
    return StudioExecutionLease(backend, owner_id=owner, ttl_seconds=ttl), backend


class TestStudioExecutionLease:
    @pytest.mark.asyncio
    async def test_acquire_heartbeat_release_roundtrip(self) -> None:
        lease, backend = _lease("A")
        epoch = await lease.acquire("t1", now=BASE)
        assert epoch == 1
        # Heartbeat renews and keeps the same epoch (renew never bumps).
        assert await lease.heartbeat(
            "t1", epoch, now=BASE + timedelta(seconds=10),
        ) is True
        await lease.release("t1")
        # Release vacates ownership under the namespaced lease name.
        current = await backend.current("studio:t1")
        assert current is not None and current[0] == ""

    @pytest.mark.asyncio
    async def test_second_owner_denied_while_live(self) -> None:
        backend = InMemoryBackgroundCoordinatorLease()
        a = StudioExecutionLease(backend, owner_id="A", ttl_seconds=100)
        b = StudioExecutionLease(backend, owner_id="B", ttl_seconds=100)
        assert await a.acquire("t1", now=BASE) == 1
        # A live lease held by A denies B.
        assert await b.acquire("t1", now=BASE + timedelta(seconds=1)) is None

    @pytest.mark.asyncio
    async def test_takeover_after_expiry_bumps_epoch_and_fails_old_heartbeat(
        self,
    ) -> None:
        backend = InMemoryBackgroundCoordinatorLease()
        a = StudioExecutionLease(backend, owner_id="A", ttl_seconds=100)
        b = StudioExecutionLease(backend, owner_id="B", ttl_seconds=100)
        assert await a.acquire("t1", now=BASE) == 1
        # After the TTL lapses, B takes over — epoch bumps to 2.
        after = BASE + timedelta(seconds=101)
        assert await b.acquire("t1", now=after) == 2
        # A's heartbeat now fails: it is no longer the owner.
        assert await a.heartbeat(
            "t1", 1, now=after + timedelta(seconds=1),
        ) is False

    @pytest.mark.asyncio
    async def test_heartbeat_rejects_stale_epoch(self) -> None:
        lease, _ = _lease("A")
        await lease.acquire("t1", now=BASE)
        # Holding epoch 1 but claiming epoch 99 must not renew.
        assert await lease.heartbeat(
            "t1", 99, now=BASE + timedelta(seconds=1),
        ) is False


class _LostHeartbeatLease:
    """Duck-typed lease whose heartbeat always reports the lease lost."""

    ttl_seconds = 30

    async def acquire(self, target_id, *, now=None):  # noqa: ANN001, ARG002
        return 1

    async def heartbeat(self, target_id, epoch, *, now=None):  # noqa: ANN001, ARG002
        return False

    async def release(self, target_id):  # noqa: ANN001
        return None


class TestStudioLeaseSession:
    @pytest.mark.asyncio
    async def test_null_session_always_acquired(self) -> None:
        async with StudioLeaseSession(None, "t1") as session:
            assert session.acquired is True
            session.raise_if_lost()  # no-op
            assert session.lost is False

    @pytest.mark.asyncio
    async def test_session_acquires_and_releases(self) -> None:
        lease, backend = _lease("A")
        async with StudioLeaseSession(
            lease, "t1", heartbeat_interval_seconds=1000,
        ) as session:
            assert session.acquired is True
            current = await backend.current("studio:t1")
            assert current is not None and current[0] == "A"
        # Exit released ownership.
        current = await backend.current("studio:t1")
        assert current is not None and current[0] == ""

    @pytest.mark.asyncio
    async def test_session_not_acquired_when_held_elsewhere(self) -> None:
        backend = InMemoryBackgroundCoordinatorLease()
        a = StudioExecutionLease(backend, owner_id="A", ttl_seconds=100)
        b = StudioExecutionLease(backend, owner_id="B", ttl_seconds=100)
        await b.acquire("t1")
        async with StudioLeaseSession(a, "t1") as session:
            assert session.acquired is False
            session.raise_if_lost()  # a non-acquired session never trips lost

    @pytest.mark.asyncio
    async def test_session_heartbeat_loss_trips_abort(self) -> None:
        async with StudioLeaseSession(
            _LostHeartbeatLease(),  # type: ignore[arg-type]
            "t1",
            heartbeat_interval_seconds=0.001,
        ) as session:
            assert session.acquired is True
            for _ in range(200):
                if session.lost:
                    break
                await asyncio.sleep(0.005)
            assert session.lost is True
            with pytest.raises(StudioLeaseLost):
                session.raise_if_lost()


class TestLeaseNameClamp:
    """``BackgroundRuntimeLeaseRow.name`` is String(64); SQLite stores an
    overlong name silently while PostgreSQL rejects it. The lease must clamp
    composite target ids (``story_event:{uuid}:{date}`` under ``studio:`` is
    66 chars) so a claim never fails only on the dialect that matters."""

    @pytest.mark.asyncio
    async def test_short_names_keep_readable_form(self) -> None:
        lease, backend = _lease("A")
        await lease.acquire("t1", now=BASE)
        assert await backend.current("studio:t1") is not None

    @pytest.mark.asyncio
    async def test_overlong_target_id_is_clamped_and_stable(self) -> None:
        overlong = "story_event:" + "c" * 36 + ":2026-07-26"
        backend = InMemoryBackgroundCoordinatorLease()
        a = StudioExecutionLease(backend, owner_id="A", ttl_seconds=100)
        b = StudioExecutionLease(backend, owner_id="B", ttl_seconds=100)
        assert len("studio:" + overlong) > 64
        assert await a.acquire(overlong, now=BASE) == 1
        # Cross-replica: B computes the SAME clamped key and is denied.
        assert await b.acquire(overlong, now=BASE + timedelta(seconds=1)) is None
        # Heartbeat + release address the same clamped key.
        assert await a.heartbeat(overlong, 1, now=BASE + timedelta(seconds=2))
        await a.release(overlong)
        assert await b.acquire(overlong, now=BASE + timedelta(seconds=3)) == 2

    @pytest.mark.asyncio
    async def test_clamped_name_within_column_bound(self) -> None:
        from kokoro_link.application.services.runtime_claim import (
            LEASE_NAME_MAX_LENGTH,
        )
        lease, _ = _lease("A")
        overlong = "x" * 200
        name = lease._name(overlong)
        assert len(name) <= LEASE_NAME_MAX_LENGTH
        assert name.startswith("studio:")
        # Deterministic across instances/restarts.
        assert name == lease._name(overlong)
        # Distinct ids stay distinct after clamping.
        assert name != lease._name("y" * 200)
