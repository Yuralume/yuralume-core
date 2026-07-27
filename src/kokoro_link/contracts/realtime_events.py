"""Ports and contracts for the realtime transactional outbox (Phase 4 §7.1).

Once Hosted splits into ``api`` and ``background`` processes the in-memory
``ProactiveEventBus`` / ``FeedEventBus`` are process-local and invisible across
processes (§7.0). Phase 4 replaces the interim SSE relay with a PostgreSQL
**transactional outbox**:

1. The background/worker process writes its domain rows AND the matching event
   row in the **same transaction** (:meth:`RealtimeOutboxPort.append` — it
   never commits on its own; it rides the caller's unit of work). If the domain
   write rolls back, the event row rolls back with it — no phantom event.
2. After that transaction commits the writer fires :meth:`notify_hint`, a
   PostgreSQL ``NOTIFY`` that is *only* a wake-up hint. It may be lost; the row
   is the durable message, never the notification payload.
3. Each API replica reads event rows (:meth:`read_after`) and re-publishes them
   onto its own local bus so its held EventSource connections see them.
4. TTL prune (:meth:`prune`) keeps the table bounded.

Out-of-order-commit anti-loss (the crux — see :meth:`read_after`): an id taken
earlier (lower ``id``) may commit *later* than a higher id. A reader that only
walks forward by ``id`` would skip the late low-id row forever. So the read
carries an **overlap window** by ``created_at`` and the caller de-duplicates by
event id.

Payload discipline (§3.1 / §11 red line): ``payload`` carries IDs / kinds /
dates / versions ONLY — never prompts, provider tokens, or chat/message body
text. The proactive message text and feed content already live in their own
durable tables; an API replica reconstructs the full bus event by re-reading
those tables from the ids in the payload, so the outbox row never duplicates
(or leaks) content. See the per-kind minimal-payload notes below.

Self-host red line: the default backend stays the in-memory bus; the
PostgreSQL adapter is only wired in under the distributed backend.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Mapping, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession


REALTIME_EVENT_CHANNEL = "yuralume_realtime_events"
"""Well-known PostgreSQL ``LISTEN`` / ``NOTIFY`` channel for the outbox wake
hint. A single channel for every replica; the notification is a best-effort
"something changed" nudge — the durable message is always the row itself, so
the payload is intentionally tiny (a max-id hint) and may be dropped."""

# Event-kind constants. The port stays schema-generic (``payload`` is an opaque
# ID/date/version Mapping); these name the three bus streams the Phase 4
# dispatcher bridges and pin the MINIMAL payload each must carry so the red line
# holds. Keep these in lockstep with the dispatcher that re-hydrates them.
EVENT_KIND_PROACTIVE = "proactive"
"""Proactive web delivery. Minimal payload: ``character_id``,
``conversation_id``, ``unread_count``. The message TEXT is NOT carried — the API
replica re-reads it from the persisted assistant message before publishing a
``ProactiveEvent``."""

EVENT_KIND_FEED_POST = "feed_post"
"""Feed post. Minimal payload: ``character_id``, ``post_id``. Content / image
url are re-read from the ``feed_posts`` row, never carried here."""

EVENT_KIND_FEED_COMMENT_REPLY = "feed_comment_reply"
"""Character reply landed. Minimal payload: ``character_id``, ``post_id``,
``comment_id``, ``unread_count``. Reply TEXT is re-read from the comment row."""

DEFAULT_OVERLAP = timedelta(seconds=30)
"""Default :meth:`RealtimeOutboxPort.read_after` overlap window. Must exceed the
worst-case commit lag between a row's ``id`` being assigned and its transaction
committing, or a late low-id row can still slip past the cursor. 30s mirrors the
background reservation window used elsewhere in the coordinator."""


@dataclass(frozen=True, slots=True)
class RealtimeEventSpec:
    """Everything needed to append one outbox event within a domain write.

    ``created_at`` is the caller's clock (the same instant the domain rows are
    stamped with) so the fencing/overlap decision and the persisted timestamp
    share one clock — never a server-side ``now()`` the reader can't reproduce.
    """

    event_kind: str
    payload: Mapping[str, Any]
    created_at: datetime
    tenant_id: str | None = None
    operator_id: str | None = None

    def __post_init__(self) -> None:
        if not self.event_kind:
            raise ValueError("RealtimeEventSpec.event_kind must be non-empty")


@dataclass(frozen=True, slots=True)
class StoredRealtimeEvent:
    """Full outbox row view returned by :meth:`append` / :meth:`read_after`."""

    id: int
    event_kind: str
    payload: Mapping[str, Any]
    created_at: datetime
    tenant_id: str | None = None
    operator_id: str | None = None


@runtime_checkable
class RealtimeOutboxPort(Protocol):
    """Durable realtime-event outbox with out-of-order-safe replay reads."""

    async def append(
        self, session: AsyncSession, spec: RealtimeEventSpec,
    ) -> StoredRealtimeEvent:
        """Write one event row **inside the caller's session/transaction**.

        Flushes (to populate the autoincrement id) but MUST NOT commit — the
        row shares the caller's unit of work, so a domain rollback drops the
        event too. Returns the stored view (with its assigned ``id``). The
        in-memory adapter accepts ``session`` for signature parity and ignores
        it (its append is immediate).
        """
        ...

    async def read_after(
        self,
        cursor_id: int,
        *,
        overlap: timedelta = DEFAULT_OVERLAP,
        limit: int,
    ) -> list[StoredRealtimeEvent]:
        """Return events for replay, ordered by ``id`` ascending.

        Returns the union of two **independently bounded** reads, each capped at
        ``limit`` and merged id-ascending:

        * the forward frontier — ``id > cursor_id``, ordered by id; and
        * the out-of-order-commit overlap tail — ``id <= cursor_id`` **but**
          ``created_at >= anchor - overlap`` (``anchor`` is the ``created_at`` of
          the ``cursor_id`` row, the newest event the caller advanced past),
          bounded to the ``limit`` ids nearest the cursor. A row taken earlier
          (lower id) but committed later is invisible when the cursor first
          advanced; because its ``created_at`` sits within ``overlap`` of the
          cursor row it is re-surfaced here.

        The two reads are deliberately separate — a single ``id > cursor OR
        created_at >= threshold`` query truncated by one ``LIMIT`` sorts the
        overlap tail (lower ids) ahead of the frontier (higher ids), so an
        overlap window holding ``>= limit`` rows consumes the whole page and the
        frontier never advances (the cursor wedges). Splitting the reads
        guarantees the frontier always gets its own ``limit`` worth of forward
        progress.

        The caller therefore MUST de-duplicate by event ``id`` (it will re-see
        rows in the overlap tail it already processed) — this is the whole
        point: without the overlap window an id-only cursor permanently skips a
        late low-id commit; the seen-id set is what makes re-delivery idempotent.

        ``cursor_id <= 0`` means "from the beginning": return the earliest rows
        by ``id`` (overlap is irrelevant). If the ``cursor_id`` row itself has
        been pruned the time anchor is unavailable, so the read degrades to the
        forward frontier only (``id > cursor_id``) — the pruned overlap tail is
        old enough that re-delivery is moot.
        """
        ...

    async def latest_id(self) -> int:
        """Return the current maximum event ``id`` (``0`` when the table is
        empty).

        A dispatcher / SSE replica initialises its cursor to this value on
        start-up so a freshly-launched process does NOT replay the whole
        history — it only forwards events appended *after* it came up. This is
        orthogonal to SSE ``Last-Event-ID`` replay (which deliberately re-reads
        from a client-supplied id). Read-only; opens its own short session in
        the SA adapter.
        """
        ...

    async def notify_hint(self, *, event_id: int | None = None) -> None:
        """Best-effort ``NOTIFY`` on :data:`REALTIME_EVENT_CHANNEL` after the
        caller's transaction has committed. A wake hint only — never a durable
        message. Any failure is swallowed with a log line (a dropped hint just
        means a replica falls back to its poll interval). No-op for in-memory.
        """
        ...

    async def prune(self, older_than: datetime) -> int:
        """Delete events with ``created_at < older_than``. Returns rows removed."""
        ...
