"""Embedded tick journal wiring (HOSTED_CORE_SCALING §13 Phase 2).

When (and only when) both the journal port and ``bucket_seconds`` are wired, a
tick records exactly its processed character set + one social row for the tick's
bucket, right after the subscription filter. Unwired → zero writes (the
byte-parity guard for the self-host red line). Journal failure is fail-soft.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import pytest

from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.contracts.clock import ensure_utc
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryTickJournal,
)
from tests.unit._messaging_harness import build_messaging_harness, create_character

pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)
BUCKET = 300


@dataclass(slots=True)
class _FrozenClock:
    value: datetime

    def now(self) -> datetime:
        return ensure_utc(self.value)


class _RecordingDispatcher:
    async def evaluate(self, *, character_id, trigger, now=None):  # noqa: ANN001
        return None


def _scheduler(harness, *, journal=None, bucket_seconds=None):
    return ProactiveScheduler(
        dispatcher=_RecordingDispatcher(),  # type: ignore[arg-type]
        character_repository=harness.character_repository,
        tick_seconds=999.0,
        startup_grace_seconds=0.0,
        clock=_FrozenClock(BASE),
        tick_journal=journal,
        bucket_seconds=bucket_seconds,
    )


async def test_wired_scheduler_records_processed_set_plus_social() -> None:
    harness = build_messaging_harness()
    c1 = await create_character(harness, name="A")
    c2 = await create_character(harness, name="B")
    journal = InMemoryTickJournal()
    scheduler = _scheduler(harness, journal=journal, bucket_seconds=BUCKET)

    await scheduler._run_tick({})  # noqa: SLF001

    rows = await journal.list_range(from_bucket=0, to_bucket=10**12)
    bucket = int(BASE.timestamp()) // BUCKET
    assert {(b, k, c) for b, k, c, _ in rows} == {
        (bucket, "character_tick", c1.id),
        (bucket, "character_tick", c2.id),
        (bucket, "social_tick", None),
    }


async def test_journal_unwired_writes_nothing() -> None:
    # bucket_seconds unset (journal present) → no writes: the tick is byte
    # identical to self-host.
    harness = build_messaging_harness()
    await create_character(harness, name="A")
    journal = InMemoryTickJournal()
    scheduler = _scheduler(harness, journal=journal, bucket_seconds=None)

    await scheduler._run_tick({})  # noqa: SLF001

    assert await journal.list_range(from_bucket=0, to_bucket=10**12) == []


async def test_journal_failure_is_fail_soft() -> None:
    harness = build_messaging_harness()
    await create_character(harness, name="A")

    class _ExplodingJournal:
        async def record(self, entries, *, now):  # noqa: ANN001
            raise RuntimeError("journal down")

        async def prune(self, *, now):  # noqa: ANN001
            return 0

        async def list_range(self, *, from_bucket, to_bucket):  # noqa: ANN001
            return []

    scheduler = _scheduler(
        harness, journal=_ExplodingJournal(), bucket_seconds=BUCKET,
    )

    # A journal crash must not propagate out of the tick.
    active, _frozen, _total = await scheduler._run_tick({})  # noqa: SLF001
    assert active == 1
