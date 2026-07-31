"""PostgreSQL integration tests for the realtime outbox adapter (§7.1 / §14).

Proves the behaviours only a real database can: the event row sharing the
caller's transaction (rollback drops it), the out-of-order-commit anti-loss
window (a lower-id row that commits AFTER the reader advanced past a higher id
is still recovered and de-duped by seen-id), TTL prune, and that ``notify_hint``
actually emits a ``NOTIFY`` a ``LISTEN`` receives. Requires Docker
(testcontainers); skipped automatically when Docker is unavailable.

Times are self-referential data anchored to a fixed base; ``read_after`` anchors
its overlap window to the cursor row's own ``created_at``, never the wall clock.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from kokoro_link.contracts.realtime_events import (
    EVENT_KIND_FEED_POST,
    EVENT_KIND_PROACTIVE,
    REALTIME_EVENT_CHANNEL,
    RealtimeEventSpec,
)
from kokoro_link.infrastructure.persistence.models import (
    AppPreferenceRow,
    RealtimeEventRow,
)
from kokoro_link.infrastructure.persistence.sa_realtime_events import (
    SARealtimeOutbox,
)


pytestmark = pytest.mark.asyncio

BASE = datetime(2026, 7, 23, 12, 0, 0, tzinfo=timezone.utc)


def _spec(*, offset_seconds: int, kind: str = EVENT_KIND_PROACTIVE, **payload):
    return RealtimeEventSpec(
        event_kind=kind,
        payload=payload or {"character_id": "char-1"},
        created_at=BASE + timedelta(seconds=offset_seconds),
    )


async def _count_events(session_factory) -> int:
    async with session_factory() as session:
        return int(
            (await session.execute(
                select(func.count()).select_from(RealtimeEventRow),
            )).scalar_one(),
        )


async def test_domain_write_and_event_share_transaction_commit(
    session_factory,
) -> None:
    outbox = SARealtimeOutbox(session_factory)
    async with session_factory() as session:
        session.add(AppPreferenceRow(
            key="pref-a", value='"v"', updated_at=BASE,
        ))
        stored = await outbox.append(session, _spec(offset_seconds=0))
        assert stored.id > 0
        await session.commit()

    # Both the domain row and the event row are durable.
    assert await _count_events(session_factory) == 1
    async with session_factory() as session:
        pref = await session.get(AppPreferenceRow, "pref-a")
        assert pref is not None


async def test_rollback_leaves_no_event_row(session_factory) -> None:
    outbox = SARealtimeOutbox(session_factory)
    async with session_factory() as session:
        session.add(AppPreferenceRow(
            key="pref-b", value='"v"', updated_at=BASE,
        ))
        await outbox.append(session, _spec(offset_seconds=0))
        await session.rollback()

    # The event row rolled back with the domain write — no phantom event.
    assert await _count_events(session_factory) == 0
    async with session_factory() as session:
        assert await session.get(AppPreferenceRow, "pref-b") is None


async def test_unserializable_payload_fails_without_durable_empty_object(
    session_factory,
) -> None:
    outbox = SARealtimeOutbox(session_factory)
    bad = RealtimeEventSpec(
        event_kind=EVENT_KIND_PROACTIVE,
        payload={"not_json": {"a", "set"}},
        created_at=BASE,
    )

    async with session_factory() as session:
        with pytest.raises(TypeError, match="JSON-serializable"):
            await outbox.append(session, bad)
        await session.rollback()

    assert await _count_events(session_factory) == 0


async def test_out_of_order_commit_is_recovered_by_overlap_window(
    session_factory,
) -> None:
    # The crux: transaction A takes the LOWER id (flushes first) but commits
    # AFTER B. A reader that advanced its cursor past B (higher id) would skip A
    # forever on an id-only cursor. The overlap window re-reads A; seen-id
    # de-dup keeps B from being re-delivered.
    outbox = SARealtimeOutbox(session_factory)

    async with session_factory() as sa:
        # A appends first → lower id, earlier created_at, but stays uncommitted.
        ev_a = await outbox.append(sa, _spec(offset_seconds=0, tag="A"))
        async with session_factory() as sb:
            ev_b = await outbox.append(sb, _spec(offset_seconds=1, tag="B"))
            await sb.commit()  # B commits first
        assert ev_a.id < ev_b.id  # A owns the lower id

        # Reader sees only the committed B and advances its cursor to B.
        seen: set[int] = set()
        first = await outbox.read_after(
            0, overlap=timedelta(seconds=30), limit=10,
        )
        assert [e.id for e in first] == [ev_b.id]
        seen.update(e.id for e in first)
        cursor = max(seen)

        await sa.commit()  # A commits LATE, after the cursor already passed it

    # Second poll from the advanced cursor: the overlap window re-surfaces the
    # late low-id A; seen-id de-dup delivers A once and never re-delivers B.
    second = await outbox.read_after(
        cursor, overlap=timedelta(seconds=30), limit=10,
    )
    newly = [e.id for e in second if e.id not in seen]
    assert newly == [ev_a.id]  # the late low-id commit is recovered
    assert ev_b.id in {e.id for e in second}  # B re-read but de-duped by caller


async def test_saturated_overlap_window_never_starves_the_frontier(
    session_factory,
) -> None:
    # Regression (sol #5): with an overlap window holding >= limit rows the
    # single ``OR ... LIMIT`` query returned only the low-id overlap tail and the
    # cursor could never advance past a burst (dispatcher wedge). The split
    # frontier/overlap reads must always surface the forward frontier.
    outbox = SARealtimeOutbox(session_factory)
    async with session_factory() as session:
        ids = [
            (await outbox.append(session, _spec(offset_seconds=i))).id
            for i in range(4)  # four rows one second apart
        ]
        await session.commit()

    # Cursor on the 3rd row (index 2). The overlap window (30s) covers all four
    # rows; the tail below the cursor is [ids[0], ids[1], ids[2]] == limit (3).
    cursor = ids[2]
    events = await outbox.read_after(
        cursor, overlap=timedelta(seconds=30), limit=3,
    )
    got = [e.id for e in events]
    # The forward frontier row ids[3] is present despite the saturated tail — the
    # old union query would have yielded only the three low ids and wedged.
    assert ids[3] in got
    assert max(got) > cursor  # a caller advancing to max(got) makes progress


async def test_prune_removes_only_older_than_cutoff(session_factory) -> None:
    outbox = SARealtimeOutbox(session_factory)
    async with session_factory() as session:
        for i in range(4):
            await outbox.append(session, _spec(offset_seconds=i))
        await session.commit()

    removed = await outbox.prune(BASE + timedelta(seconds=2))
    assert removed == 2
    remaining = await outbox.read_after(0, limit=10)
    assert [e.created_at for e in remaining] == [
        BASE + timedelta(seconds=2), BASE + timedelta(seconds=3),
    ]


async def test_pruned_cursor_falls_back_to_forward_frontier(
    session_factory,
) -> None:
    outbox = SARealtimeOutbox(session_factory)
    async with session_factory() as session:
        ids = [
            (await outbox.append(session, _spec(offset_seconds=i))).id
            for i in range(4)
        ]
        await session.commit()

    # Prune the first two; then read from a now-pruned cursor id.
    await outbox.prune(BASE + timedelta(seconds=2))
    events = await outbox.read_after(
        ids[0], overlap=timedelta(seconds=30), limit=10,
    )
    assert [e.id for e in events] == [ids[2], ids[3]]


async def test_notify_hint_emits_a_notification(
    session_factory, postgres_container,
) -> None:
    import asyncpg

    dsn = postgres_container.get_connection_url()
    for marker in ("+psycopg2", "+asyncpg"):
        dsn = dsn.replace(marker, "")

    received: asyncio.Queue[str] = asyncio.Queue()
    conn = await asyncpg.connect(dsn)
    try:
        await conn.add_listener(
            REALTIME_EVENT_CHANNEL,
            lambda _c, _pid, _channel, payload: received.put_nowait(payload),
        )
        outbox = SARealtimeOutbox(session_factory)
        await outbox.notify_hint(event_id=42)
        payload = await asyncio.wait_for(received.get(), timeout=5.0)
        # Content is a best-effort hint only, not a durable dependency; we only
        # assert the channel fired and the wake hint carried the id.
        assert payload == "42"
    finally:
        await conn.close()
