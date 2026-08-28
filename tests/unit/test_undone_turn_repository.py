"""Behaviour pins for the undo-tombstone store (TU1).

The in-memory adapter is what every wave-2 test of the post-turn gate
will run against, so the three properties the gate relies on are pinned
here rather than left to be discovered by whoever writes that gate:

* recording is idempotent **and first-write-wins** — a repeat undo must
  not push ``undone_at`` forward and quietly extend the row's life past
  the GC window it was already measured against;
* the lookup is exact, so an unrelated turn is never gated;
* ``prune`` is age-based and strictly-less-than, so the boundary row
  survives its own cutoff.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.domain.entities.undone_turn import UndoneTurn
from kokoro_link.infrastructure.repositories.in_memory_undone_turns import (
    InMemoryUndoneTurnRepository,
)

_NOW = datetime(2026, 8, 25, 9, 30, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_recorded_turn_is_gated_and_others_are_not() -> None:
    repo = InMemoryUndoneTurnRepository()
    await repo.record(UndoneTurn.new(
        turn_record_id="turn-1", conversation_id="conv-1", undone_at=_NOW,
    ))

    assert await repo.is_undone("turn-1") is True
    assert await repo.is_undone("turn-2") is False


@pytest.mark.asyncio
async def test_recording_twice_keeps_the_first_timestamp() -> None:
    repo = InMemoryUndoneTurnRepository()
    await repo.record(UndoneTurn.new(
        turn_record_id="turn-1", conversation_id="conv-1", undone_at=_NOW,
    ))
    await repo.record(UndoneTurn.new(
        turn_record_id="turn-1",
        conversation_id="conv-1",
        undone_at=_NOW + timedelta(days=3),
    ))

    # The later write did not move the clock forward: pruning against a
    # cutoff after the *first* stamp still collects the row.
    assert await repo.prune(older_than=_NOW + timedelta(hours=1)) == 1
    assert await repo.is_undone("turn-1") is False


@pytest.mark.asyncio
async def test_prune_drops_only_rows_older_than_the_cutoff() -> None:
    repo = InMemoryUndoneTurnRepository()
    for offset, key in ((-2, "old"), (0, "boundary"), (2, "fresh")):
        await repo.record(UndoneTurn.new(
            turn_record_id=key,
            conversation_id="conv-1",
            undone_at=_NOW + timedelta(days=offset),
        ))

    removed = await repo.prune(older_than=_NOW)

    assert removed == 1
    assert await repo.is_undone("old") is False
    # Strictly-less-than: dropping a tombstone early re-opens exactly the
    # race it exists to close, so the boundary belongs to the survivors.
    assert await repo.is_undone("boundary") is True
    assert await repo.is_undone("fresh") is True


@pytest.mark.asyncio
async def test_delete_for_conversation_is_scoped() -> None:
    repo = InMemoryUndoneTurnRepository()
    await repo.record(UndoneTurn.new(
        turn_record_id="turn-1", conversation_id="conv-1", undone_at=_NOW,
    ))
    await repo.record(UndoneTurn.new(
        turn_record_id="turn-2", conversation_id="conv-2", undone_at=_NOW,
    ))

    assert await repo.delete_for_conversation("conv-1") == 1
    assert await repo.is_undone("turn-1") is False
    assert await repo.is_undone("turn-2") is True


def test_naive_timestamps_are_normalised_to_utc() -> None:
    """SQLite hands back naive datetimes for a ``DateTime(timezone=True)``
    column; a naive ``undone_at`` reaching the prune comparison would
    raise instead of collecting the row."""
    tombstone = UndoneTurn.new(
        turn_record_id="turn-1",
        conversation_id="conv-1",
        undone_at=datetime(2026, 8, 25, 9, 30),
    )

    assert tombstone.undone_at.tzinfo is timezone.utc


@pytest.mark.parametrize(
    ("turn_record_id", "conversation_id"),
    [("", "conv-1"), ("turn-1", "")],
)
def test_a_tombstone_without_an_anchor_is_rejected(
    turn_record_id: str, conversation_id: str,
) -> None:
    """An empty key would record a gate that matches nothing while
    reporting success — the worst possible failure mode for an
    interlock."""
    with pytest.raises(ValueError):
        UndoneTurn.new(
            turn_record_id=turn_record_id,
            conversation_id=conversation_id,
            undone_at=_NOW,
        )
