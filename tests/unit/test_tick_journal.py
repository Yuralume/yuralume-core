"""Unit tests for the scheduler-tick journal semantics."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryTickJournal,
)


pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


async def test_record_is_idempotent_and_list_range_is_deterministic() -> None:
    journal = InMemoryTickJournal()
    await journal.record(
        [
            (100, "character_tick", "char-1"),
            (100, "character_tick", "char-1"),
            (100, "social_tick", None),
            (100, "social_tick", None),
            (100, "character_tick", "char-2"),
        ],
        now=BASE,
    )
    await journal.record(
        [(100, "character_tick", "char-1"), (100, "social_tick", None)],
        now=BASE + timedelta(seconds=1),
    )

    rows = await journal.list_range(from_bucket=100, to_bucket=100)
    assert [(bucket, kind, character_id) for bucket, kind, character_id, _ in rows] == [
        (100, "character_tick", "char-1"),
        (100, "character_tick", "char-2"),
        (100, "social_tick", None),
    ]
    assert all(recorded_at == BASE for *_, recorded_at in rows)
    assert await journal.earliest_bucket_after(after_bucket=99, to_bucket=10**12) == 100
    assert await journal.list_bucket(100) == rows


async def test_earliest_bucket_ignores_empty_buckets_and_exact_list_is_bounded() -> None:
    journal = InMemoryTickJournal()
    await journal.record(
        [(10, "character_tick", "char-1"), (10**12, "character_tick", "char-2")],
        now=BASE,
    )

    assert await journal.earliest_bucket_after(
        after_bucket=10, to_bucket=10**12,
    ) == 10**12
    assert await journal.earliest_bucket_after(
        after_bucket=10**12, to_bucket=10**12 + 1,
    ) is None
    assert await journal.list_bucket(11) == []


    await journal.record(
        [
            (100, "character_tick", "char-1"),
            (100, "character_tick", "char-2"),
            (100, "social_tick", None),
            (200, "character_tick", "char-1"),
        ],
        now=BASE,
    )

    in_range = await journal.list_range(from_bucket=100, to_bucket=150)
    assert len(in_range) == 3
    kinds = {(bucket, kind, cid) for bucket, kind, cid, _ in in_range}
    assert kinds == {
        (100, "character_tick", "char-1"),
        (100, "character_tick", "char-2"),
        (100, "social_tick", None),
    }
    # social_tick carries no character id.
    social = [row for row in in_range if row[1] == "social_tick"]
    assert social[0][2] is None


async def test_prune_removes_entries_older_than_7_days() -> None:
    journal = InMemoryTickJournal()
    await journal.record([(1, "character_tick", "char-1")], now=BASE)
    await journal.record(
        [(2, "character_tick", "char-1")], now=BASE + timedelta(days=6),
    )

    removed = await journal.prune(now=BASE + timedelta(days=8))
    assert removed == 1
    remaining = await journal.list_range(from_bucket=0, to_bucket=100)
    assert [r[0] for r in remaining] == [2]
