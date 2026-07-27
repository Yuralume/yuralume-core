"""Unit tests for the realtime transactional outbox (Phase 4 §7.1).

Covers the in-memory adapter's parity semantics — append id assignment, the
out-of-order-safe ``read_after`` overlap window + seen-id de-dup, prune, the
pruned-cursor fallback — and the ``YURALUME_REALTIME_BACKEND`` settings parse.
The SQLAlchemy adapter is exercised against a real PostgreSQL in
``tests/integration/test_sa_realtime_events.py`` (this repo has no aiosqlite
test dependency, so there is no offline SA path here).

Times are self-referential data anchored to a fixed base and never compared to
the wall clock — ``read_after`` anchors its window to the cursor row's own
``created_at``, so the semantics are deterministic without a real now().
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.bootstrap.process_settings import load_process_settings
from kokoro_link.contracts.realtime_events import (
    EVENT_KIND_PROACTIVE,
    RealtimeEventSpec,
)
from kokoro_link.infrastructure.repositories.in_memory_realtime_events import (
    InMemoryRealtimeOutbox,
)


BASE = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


def _spec(*, offset_seconds: int, kind: str = EVENT_KIND_PROACTIVE, **payload):
    return RealtimeEventSpec(
        event_kind=kind,
        payload=payload or {"character_id": "char-1"},
        created_at=BASE + timedelta(seconds=offset_seconds),
    )


@pytest.mark.asyncio
async def test_append_assigns_monotonic_ids_and_returns_stored_view() -> None:
    outbox = InMemoryRealtimeOutbox()
    e1 = await outbox.append(None, _spec(offset_seconds=0, conversation_id="c1"))
    e2 = await outbox.append(None, _spec(offset_seconds=1, conversation_id="c2"))
    assert e1.id == 1 and e2.id == 2
    assert e1.event_kind == EVENT_KIND_PROACTIVE
    assert e1.payload == {"conversation_id": "c1"}
    assert e1.created_at == BASE


@pytest.mark.asyncio
async def test_read_from_beginning_returns_all_by_id() -> None:
    outbox = InMemoryRealtimeOutbox()
    for i in range(3):
        await outbox.append(None, _spec(offset_seconds=i))
    events = await outbox.read_after(0, limit=10)
    assert [e.id for e in events] == [1, 2, 3]


@pytest.mark.asyncio
async def test_read_after_limit_is_respected() -> None:
    outbox = InMemoryRealtimeOutbox()
    for i in range(5):
        await outbox.append(None, _spec(offset_seconds=i))
    events = await outbox.read_after(0, limit=2)
    assert [e.id for e in events] == [1, 2]


@pytest.mark.asyncio
async def test_overlap_window_resurfaces_low_id_within_window() -> None:
    # The anti-loss contract: a row with id <= cursor whose created_at sits
    # within the overlap of the cursor row's created_at MUST be re-read, so a
    # late-committing low-id row is never permanently skipped.
    outbox = InMemoryRealtimeOutbox()
    await outbox.append(None, _spec(offset_seconds=0))   # id=1, t+0
    await outbox.append(None, _spec(offset_seconds=2))   # id=2, t+2 (cursor)
    await outbox.append(None, _spec(offset_seconds=3))   # id=3, t+3

    # Cursor at id=2 (created_at t+2), generous 5s overlap → threshold t-3s.
    events = await outbox.read_after(2, overlap=timedelta(seconds=5), limit=10)
    # id=3 via the forward frontier; id=1 & id=2 via the overlap re-read.
    assert [e.id for e in events] == [1, 2, 3]


@pytest.mark.asyncio
async def test_overlap_window_excludes_rows_outside_window() -> None:
    outbox = InMemoryRealtimeOutbox()
    await outbox.append(None, _spec(offset_seconds=0))   # id=1, t+0
    await outbox.append(None, _spec(offset_seconds=2))   # id=2, t+2 (cursor)
    await outbox.append(None, _spec(offset_seconds=3))   # id=3, t+3

    # Zero overlap → threshold == cursor created_at (t+2): only id>=cursor's
    # timestamp survive; the older id=1 (t+0) is outside the window.
    events = await outbox.read_after(2, overlap=timedelta(seconds=0), limit=10)
    assert [e.id for e in events] == [2, 3]


@pytest.mark.asyncio
async def test_saturated_overlap_window_never_starves_the_frontier() -> None:
    # Regression (sol #5): a full overlap window of ``>= limit`` rows must NOT
    # consume the whole page and hide the forward frontier — otherwise the
    # cursor can never advance past a burst and the dispatcher wedges.
    outbox = InMemoryRealtimeOutbox()
    # Four rows one second apart; cursor sits on id=3. The overlap window
    # (id<=3, created_at within 30s) holds exactly ``limit`` rows [1,2,3].
    for i in range(4):
        await outbox.append(None, _spec(offset_seconds=i))

    events = await outbox.read_after(3, overlap=timedelta(seconds=30), limit=3)
    ids = [e.id for e in events]
    # The frontier row id=4 is present despite the saturated overlap tail — the
    # old ``OR ... LIMIT`` slice would have returned only [1,2,3] and wedged.
    assert 4 in ids
    assert max(ids) > 3  # a caller advancing its cursor to max(ids) progresses


@pytest.mark.asyncio
async def test_seen_id_dedup_makes_overlap_re_delivery_idempotent() -> None:
    # A caller that de-dupes by id sees each event exactly once even though the
    # overlap window re-reads already-processed rows.
    outbox = InMemoryRealtimeOutbox()
    for i in range(3):
        await outbox.append(None, _spec(offset_seconds=i))

    seen: set[int] = set()
    delivered: list[int] = []
    # First poll from the beginning.
    for e in await outbox.read_after(0, overlap=timedelta(seconds=30), limit=10):
        if e.id not in seen:
            seen.add(e.id)
            delivered.append(e.id)
    cursor = max(seen)

    # Second poll: overlap re-reads all three, but seen-id de-dup delivers none.
    for e in await outbox.read_after(
        cursor, overlap=timedelta(seconds=30), limit=10,
    ):
        if e.id not in seen:
            seen.add(e.id)
            delivered.append(e.id)

    assert delivered == [1, 2, 3]  # each delivered once, never re-delivered


@pytest.mark.asyncio
async def test_pruned_cursor_falls_back_to_forward_frontier() -> None:
    outbox = InMemoryRealtimeOutbox()
    for i in range(4):
        await outbox.append(None, _spec(offset_seconds=i))
    # Prune the first two (created_at < BASE+2s).
    removed = await outbox.prune(BASE + timedelta(seconds=2))
    assert removed == 2

    # Cursor id=1 no longer exists → no time anchor → forward frontier only.
    events = await outbox.read_after(1, overlap=timedelta(seconds=30), limit=10)
    assert [e.id for e in events] == [3, 4]


@pytest.mark.asyncio
async def test_prune_removes_only_older_than_cutoff() -> None:
    outbox = InMemoryRealtimeOutbox()
    for i in range(4):
        await outbox.append(None, _spec(offset_seconds=i))
    removed = await outbox.prune(BASE + timedelta(seconds=2))
    assert removed == 2
    remaining = await outbox.read_after(0, limit=10)
    assert [e.id for e in remaining] == [3, 4]


@pytest.mark.asyncio
async def test_notify_hint_is_noop_in_memory() -> None:
    outbox = InMemoryRealtimeOutbox()
    # Must not raise; nothing to assert beyond that in-process.
    await outbox.notify_hint(event_id=1)
    await outbox.notify_hint()


# --- settings parse -------------------------------------------------------

def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "YURALUME_PROCESS_ROLE",
        "YURALUME_BACKGROUND_BACKEND",
        "YURALUME_BACKGROUND_SHADOW",
        "YURALUME_REALTIME_BACKEND",
    ):
        monkeypatch.delenv(key, raising=False)


def test_realtime_backend_defaults_to_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    assert load_process_settings().realtime_backend == "memory"


def test_realtime_backend_postgres_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("YURALUME_REALTIME_BACKEND", "postgres")
    assert load_process_settings().realtime_backend == "postgres"


def test_realtime_backend_invalid_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("YURALUME_REALTIME_BACKEND", "redis")
    with pytest.raises(ValueError) as excinfo:
        load_process_settings()
    message = str(excinfo.value)
    assert "YURALUME_REALTIME_BACKEND" in message
    assert "memory, postgres" in message
