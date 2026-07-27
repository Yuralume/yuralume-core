"""PostgreSQL integration for the execution-ownership CAS (P3-B §2.2 / §15).

Proves the only thing a real database can: two concurrent flips against the
same epoch (INSERT-from-absent and UPDATE) resolve to EXACTLY one winner via
``SELECT ... FOR UPDATE``, and the absent-row default reads ('embedded', 0).
Requires Docker (testcontainers); auto-skipped when unavailable.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from kokoro_link.contracts.execution_mode import MODE_DISTRIBUTED, MODE_EMBEDDED
from kokoro_link.infrastructure.persistence.sa_background_runtime import (
    SABackgroundExecutionMode,
)

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


async def test_absent_row_reads_embedded_epoch_zero(session_factory) -> None:
    port = SABackgroundExecutionMode(session_factory)
    assert await port.get() == (MODE_EMBEDDED, 0)


async def test_flip_from_absent_inserts_row(session_factory) -> None:
    port = SABackgroundExecutionMode(session_factory)
    assert await port.flip(MODE_DISTRIBUTED, 0, now=NOW) == 1
    assert await port.get() == (MODE_DISTRIBUTED, 1)


async def test_flip_stale_epoch_rejected(session_factory) -> None:
    port = SABackgroundExecutionMode(session_factory)
    await port.flip(MODE_DISTRIBUTED, 0, now=NOW)  # → epoch 1
    # Re-flipping against the stale epoch 0 must be rejected.
    assert await port.flip(MODE_EMBEDDED, 0, now=NOW) is None
    assert await port.get() == (MODE_DISTRIBUTED, 1)
    assert await port.flip(MODE_EMBEDDED, 1, now=NOW) == 2


async def test_concurrent_insert_flip_single_winner(session_factory) -> None:
    # Many tasks race to INSERT the absent row at epoch 0 — exactly one wins.
    ports = [SABackgroundExecutionMode(session_factory) for _ in range(16)]
    results = await asyncio.gather(
        *[p.flip(MODE_DISTRIBUTED, 0, now=NOW) for p in ports],
    )
    winners = [r for r in results if r is not None]
    assert winners == [1]
    assert await ports[0].get() == (MODE_DISTRIBUTED, 1)


async def test_concurrent_update_flip_single_winner(session_factory) -> None:
    # Row already exists at epoch 1; many tasks race the CAS from epoch 1.
    seed = SABackgroundExecutionMode(session_factory)
    await seed.flip(MODE_DISTRIBUTED, 0, now=NOW)  # → epoch 1
    ports = [SABackgroundExecutionMode(session_factory) for _ in range(16)]
    results = await asyncio.gather(
        *[p.flip(MODE_EMBEDDED, 1, now=NOW) for p in ports],
    )
    winners = [r for r in results if r is not None]
    assert winners == [2]  # exactly one bumped 1 → 2
    assert await seed.get() == (MODE_EMBEDDED, 2)
