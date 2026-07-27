"""RuntimeOwnershipPort semantics (P3-B, §2.2 / §15).

Absent-row default, the epoch CAS, and the concurrent-flip single-winner
invariant against the in-memory twin (the PG twin is exercised in
tests/integration/test_sa_execution_mode.py via asyncio.gather).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from kokoro_link.contracts.execution_mode import (
    MODE_DISTRIBUTED,
    MODE_EMBEDDED,
)
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryRuntimeOwnership,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


async def test_absent_row_reads_embedded_epoch_zero() -> None:
    port = InMemoryRuntimeOwnership()
    assert await port.get() == (MODE_EMBEDDED, 0)


async def test_flip_from_absent_inserts_and_bumps_epoch() -> None:
    port = InMemoryRuntimeOwnership()
    new_epoch = await port.flip(MODE_DISTRIBUTED, 0, now=NOW)
    assert new_epoch == 1
    assert await port.get() == (MODE_DISTRIBUTED, 1)


async def test_flip_stale_expected_epoch_returns_none_no_change() -> None:
    port = InMemoryRuntimeOwnership()
    # Row is absent (epoch 0); a flip expecting epoch 5 is stale.
    assert await port.flip(MODE_DISTRIBUTED, 5, now=NOW) is None
    assert await port.get() == (MODE_EMBEDDED, 0)


async def test_round_trip_flip_back_and_forth() -> None:
    port = InMemoryRuntimeOwnership()
    assert await port.flip(MODE_DISTRIBUTED, 0, now=NOW) == 1
    # Re-using the stale epoch fails.
    assert await port.flip(MODE_EMBEDDED, 0, now=NOW) is None
    # Correct epoch succeeds and bumps again.
    assert await port.flip(MODE_EMBEDDED, 1, now=NOW) == 2
    assert await port.get() == (MODE_EMBEDDED, 2)


async def test_concurrent_flip_exactly_one_winner() -> None:
    port = InMemoryRuntimeOwnership()
    results = await asyncio.gather(
        *[port.flip(MODE_DISTRIBUTED, 0, now=NOW) for _ in range(12)],
    )
    winners = [r for r in results if r is not None]
    assert winners == [1]  # exactly one flip won, and it bumped to epoch 1
    assert await port.get() == (MODE_DISTRIBUTED, 1)
