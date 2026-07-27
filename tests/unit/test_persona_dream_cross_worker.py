"""Cross-worker throttling for the persona dream pass.

``_last_run_at`` is per process. With 2×worker replicas the
``dream_min_interval_hours`` throttle was effectively absent — each worker kept
its own "last run" map, so the same (character, operator) could dream twice per
window (one expensive LLM call each) with only the weaker ``count_pending`` gate
in between. These tests pin the shared TTL cooldown claim: the claim is taken
and deliberately never released, so the TTL *is* the cooldown and it self-heals
on expiry.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from kokoro_link.application.services.persona_dream_service import (
    PersonaDreamService,
)
from kokoro_link.application.services.runtime_claim import RuntimeClaim
from kokoro_link.bootstrap.settings import PersonaSettings
from kokoro_link.contracts.persona_consolidator import ConsolidationResult
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
)

_CHAR_ID = "char-A"
_OP_ID = "operator-1"
# 03:00 sits inside the §4.5 default quiet window (02–06).
_QUIET = datetime(2026, 1, 1, 3, 0, tzinfo=timezone.utc)


def _worker(
    lease: InMemoryBackgroundCoordinatorLease | None,
    owner_id: str,
    *,
    settings: PersonaSettings | None = None,
) -> PersonaDreamService:
    """One worker replica: its own service + claim owner, shared lease table."""
    repo = AsyncMock()
    repo.count_pending = AsyncMock(return_value=99)
    repo.list_pending = AsyncMock(return_value=[])
    repo.list_confirmed_for_decay = AsyncMock(return_value=[])
    consolidator = AsyncMock()
    consolidator.consolidate = AsyncMock(return_value=ConsolidationResult())
    persona_service = MagicMock()
    persona_service.get_current = AsyncMock()
    persona_service.invalidate_cache = MagicMock()
    return PersonaDreamService(
        consolidator=consolidator,
        repository=repo,
        persona_service=persona_service,
        settings=settings or PersonaSettings(),
        cooldown_claim=(
            None if lease is None
            else RuntimeClaim(lease, prefix="dream", owner_id=owner_id)
        ),
    )


@pytest.mark.asyncio
async def test_second_worker_is_throttled_within_the_interval() -> None:
    lease = InMemoryBackgroundCoordinatorLease()
    worker_a = _worker(lease, "worker-a")
    worker_b = _worker(lease, "worker-b")

    assert await worker_a.should_run_now(_CHAR_ID, _OP_ID, now=_QUIET)
    # Same quiet window, same pair, different replica → must not dream again.
    assert not await worker_b.should_run_now(
        _CHAR_ID, _OP_ID, now=_QUIET + timedelta(minutes=5),
    )


@pytest.mark.asyncio
async def test_cooldown_lapses_after_the_configured_interval() -> None:
    settings = PersonaSettings(dream_min_interval_hours=6)
    lease = InMemoryBackgroundCoordinatorLease()
    worker_a = _worker(lease, "worker-a", settings=settings)
    worker_b = _worker(lease, "worker-b", settings=settings)

    assert await worker_a.should_run_now(_CHAR_ID, _OP_ID, now=_QUIET)
    # Still inside the same quiet window (05:00) and inside the 6h cooldown.
    assert not await worker_b.should_run_now(
        _CHAR_ID, _OP_ID, now=_QUIET + timedelta(hours=2),
    )
    # Next quiet window a full interval later — a crashed holder must not
    # keep the pair from ever dreaming again.
    assert await worker_b.should_run_now(
        _CHAR_ID, _OP_ID, now=_QUIET + timedelta(hours=24, minutes=1),
    )


@pytest.mark.asyncio
async def test_claim_is_scoped_per_character_operator_pair() -> None:
    lease = InMemoryBackgroundCoordinatorLease()
    worker_a = _worker(lease, "worker-a")
    worker_b = _worker(lease, "worker-b")

    assert await worker_a.should_run_now(_CHAR_ID, _OP_ID, now=_QUIET)
    assert await worker_b.should_run_now("char-B", _OP_ID, now=_QUIET)
    assert await worker_b.should_run_now(_CHAR_ID, "operator-2", now=_QUIET)


@pytest.mark.asyncio
async def test_cheaper_gates_run_before_the_claim_is_taken() -> None:
    """Quiet hours / pending count must reject BEFORE we burn a lease write —
    otherwise an active-hours tick would silently eat the day's cooldown."""
    lease = InMemoryBackgroundCoordinatorLease()
    worker_a = _worker(lease, "worker-a")
    worker_b = _worker(lease, "worker-b")
    noon = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)

    assert not await worker_a.should_run_now(_CHAR_ID, _OP_ID, now=noon)
    assert await worker_b.should_run_now(_CHAR_ID, _OP_ID, now=_QUIET)


@pytest.mark.asyncio
async def test_lease_less_worker_keeps_single_process_behaviour() -> None:
    """Self-host: no claim wired → the historical ``_last_run_at`` gate only."""
    worker = _worker(None, "")

    assert await worker.should_run_now(_CHAR_ID, _OP_ID, now=_QUIET)
    await worker.run_consolidation(_CHAR_ID, _OP_ID, now=_QUIET)
    assert not await worker.should_run_now(
        _CHAR_ID, _OP_ID, now=_QUIET + timedelta(hours=1),
    )


# --- same-process duplicate window ---------------------------------------- #
#
# The claim renews (and therefore succeeds) for the SAME owner over a still-live
# row — that is the right semantic for a single-flight retry, but it means the
# fleet claim cannot suppress a repeat from the process that already holds it.
# Two guards were therefore needed on this side, and both used to be missing:
# the in-memory gate had an ``await`` (``count_pending``) between its check and
# the stamp, and the stamp itself only landed at the END of a successful pass.


def _gate_count_pending(worker: PersonaDreamService, parties: int) -> None:
    """Make ``count_pending`` park until ``parties`` callers are inside it.

    Reproduces the real interleaving: both coroutines clear the cheap in-memory
    gate, then both resume into the claim. Without a check-and-stamp that has no
    ``await`` in the middle, both are told to dream.
    """
    barrier = asyncio.Barrier(parties)

    async def _count(*_args: object, **_kwargs: object) -> int:
        await barrier.wait()
        return 99

    worker._repository.count_pending = _count  # type: ignore[method-assign]


@pytest.mark.asyncio
async def test_two_coroutines_on_one_worker_get_exactly_one_go_ahead() -> None:
    lease = InMemoryBackgroundCoordinatorLease()
    worker = _worker(lease, "worker-a")
    _gate_count_pending(worker, 2)

    verdicts = await asyncio.gather(
        worker.should_run_now(_CHAR_ID, _OP_ID, now=_QUIET),
        worker.should_run_now(_CHAR_ID, _OP_ID, now=_QUIET),
    )

    assert sorted(verdicts) == [False, True]


@pytest.mark.asyncio
async def test_lease_less_worker_also_serialises_concurrent_coroutines() -> None:
    """Self-host runs one process, so this gate is the ONLY thing there is."""
    worker = _worker(None, "")
    _gate_count_pending(worker, 2)

    verdicts = await asyncio.gather(
        worker.should_run_now(_CHAR_ID, _OP_ID, now=_QUIET),
        worker.should_run_now(_CHAR_ID, _OP_ID, now=_QUIET),
    )

    assert sorted(verdicts) == [False, True]


@pytest.mark.asyncio
async def test_failed_pass_still_consumes_the_window_on_this_worker() -> None:
    """A crashed pass must not hand the same worker a free retry every tick.

    Consuming the cooldown is an ATTEMPT semantic — it matches the claim, which
    is spent the moment it is taken and lapses only by TTL. Stamping just at the
    happy-path tail let a pass that died in prep re-enter on the next tick,
    burning the pair's window on repeated failures.
    """
    lease = InMemoryBackgroundCoordinatorLease()
    worker = _worker(lease, "worker-a")
    worker._persona_service.get_current = AsyncMock(  # type: ignore[method-assign]
        side_effect=RuntimeError("prep exploded"),
    )

    assert await worker.should_run_now(_CHAR_ID, _OP_ID, now=_QUIET)
    await worker.run_consolidation(_CHAR_ID, _OP_ID, now=_QUIET)

    assert not await worker.should_run_now(
        _CHAR_ID, _OP_ID, now=_QUIET + timedelta(minutes=30),
    )
