"""Ports and contracts for the durable background-job queue (P2-A).

This is the *foundation only*: the schema, value objects and ports that
the distributed Hosted topology (coordinator / worker) will consume in
P2-B. Nothing here is wired into any running process — it is entirely
additive and dormant. See
``docs/plans/HOSTED_CORE_SCALING_ARCHITECTURE_PLAN.md`` §2.2/§3.

Design invariants that every adapter (in-memory + SQLAlchemy) MUST honour
identically so the parity tests (§14) hold:

* **Idempotency** — at most one *active* (``queued``/``claimed``) row may
  exist per ``idempotency_key``. ``enqueue`` returns ``None`` (not an
  error) when an active row already exists; the same logical key may
  recur once the previous occurrence reached a terminal status.
* **Lease + fencing** — a claimed job carries a TTL lease
  (``lease_owner`` / ``lease_until``). A worker may only ``complete`` /
  ``fail`` / ``extend_lease`` a job whose lease it still holds *and whose
  lease has not expired* (``lease_until >= now``). An expired claimed job
  is re-claimable by any worker (attempt is charged again).
* **Coordinator epoch** — ``enqueue`` validates ``spec.fencing_epoch``
  against the current ``background-coordinator`` lease *in the same
  transaction, under a row lock* (``SELECT ... FOR UPDATE``): the lease
  must still be live (``lease_until >= now``) AND its epoch must equal
  ``spec.fencing_epoch``. A partitioned ex-coordinator carrying a stale
  epoch — or one whose own lease has expired — is silently rejected
  (``None`` + warning), never allowed to insert. The row lock is what
  makes this atomic against a concurrent takeover: the takeover and the
  fencing read serialise on the lease row, so no stale row can slip in
  after a takeover commits.
* **Retention (拍板 2026-07-20)** — ``done``/``failed``/``superseded``
  rows prune after 7 days; ``dead`` rows after 30 days, with NO
  auto-retry — admin manual ``redrive`` only.

Payload discipline (§3.1 / §11): ``payload`` carries IDs / dates / config
versions ONLY — never prompts, provider tokens or chat content;
``last_error`` carries an exception class + a sanitised summary — never a
secret.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any, Mapping, Protocol, runtime_checkable


COORDINATOR_LEASE_NAME = "background-coordinator"
"""Well-known runtime-lease name that gates who may enqueue background
jobs. ``enqueue`` fences against this lease's epoch."""

DEFAULT_MAX_ATTEMPTS = 5

_TERMINAL_RETENTION_DAYS = 7
_DEAD_RETENTION_DAYS = 30

_ERROR_SUMMARY_MAX_CHARS = 500
# A run of >=20 characters with no whitespace that also contains no
# obvious sentence punctuation looks like a token / key / hash rather
# than prose — redact it defensively before it can reach a log or the
# ``last_error`` column. Deliberately conservative: prose words are far
# shorter and real errors keep their class + short message.
_TOKEN_LIKE = re.compile(r"[^\s]{20,}")


class JobStatus(StrEnum):
    """Lifecycle of a :class:`BackgroundJob`.

    ``queued`` → ``claimed`` → (``done`` | ``failed`` | ``dead``).
    ``superseded`` is a terminal marker for a logical job that a newer
    occurrence replaced. Terminal states: ``done`` / ``failed`` /
    ``dead`` / ``superseded``.
    """

    QUEUED = "queued"
    CLAIMED = "claimed"
    DONE = "done"
    FAILED = "failed"
    DEAD = "dead"
    SUPERSEDED = "superseded"


ACTIVE_STATUSES = frozenset({JobStatus.QUEUED, JobStatus.CLAIMED})
TERMINAL_STATUSES = frozenset(
    {JobStatus.DONE, JobStatus.FAILED, JobStatus.DEAD, JobStatus.SUPERSEDED},
)
# Terminal states whose rows prune on the 7-day clock (dead is 30-day).
_SHORT_RETENTION_STATUSES = frozenset(
    {JobStatus.DONE, JobStatus.FAILED, JobStatus.SUPERSEDED},
)


def summarize_error(error: BaseException | str) -> str:
    """Collapse an exception / message into a safe ``last_error`` value.

    Guarantees the column is never empty and never carries a secret: the
    result is ``"ExceptionClass: message"`` (or the raw string), with any
    token-like run redacted and the whole thing truncated. Callers must
    route *all* failure text through here before persisting it.
    """

    if isinstance(error, BaseException):
        raw = f"{type(error).__name__}: {error}".strip()
    else:
        raw = str(error).strip()
    if not raw:
        raw = "UnknownError"
    redacted = _TOKEN_LIKE.sub("[redacted]", raw)
    if len(redacted) > _ERROR_SUMMARY_MAX_CHARS:
        redacted = redacted[: _ERROR_SUMMARY_MAX_CHARS - 1] + "…"
    return redacted


@dataclass(frozen=True, slots=True)
class BackgroundJobSpec:
    """Everything needed to enqueue one durable job.

    ``fencing_epoch`` MUST equal the current coordinator lease epoch or
    the enqueue is rejected (a stale ex-coordinator cannot insert).
    """

    kind: str
    idempotency_key: str
    due_at: datetime
    fencing_epoch: int
    priority: int = 3
    tenant_id: str | None = None
    operator_id: str | None = None
    character_id: str | None = None
    payload: Mapping[str, Any] = field(default_factory=dict)
    payload_version: int = 1
    max_attempts: int = DEFAULT_MAX_ATTEMPTS

    def __post_init__(self) -> None:
        if not self.kind:
            raise ValueError("BackgroundJobSpec.kind must be non-empty")
        if not self.idempotency_key:
            raise ValueError(
                "BackgroundJobSpec.idempotency_key must be non-empty",
            )
        if self.max_attempts < 1:
            raise ValueError("BackgroundJobSpec.max_attempts must be >= 1")


@dataclass(frozen=True, slots=True)
class BackgroundJob:
    """Full row view of a durable job."""

    id: str
    kind: str
    status: JobStatus
    idempotency_key: str
    due_at: datetime
    priority: int
    fencing_epoch: int
    attempt_count: int
    max_attempts: int
    payload_version: int
    payload: Mapping[str, Any]
    created_at: datetime
    updated_at: datetime
    tenant_id: str | None = None
    operator_id: str | None = None
    character_id: str | None = None
    lease_owner: str | None = None
    lease_until: datetime | None = None
    last_error: str | None = None
    outcome: Mapping[str, Any] | None = None
    claimed_at: datetime | None = None
    finished_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class ClaimedJob:
    """The worker-facing subset returned by :meth:`claim`.

    Carries the identity, payload and fencing/lease context a handler
    needs to run and to re-validate before applying its effect.
    """

    id: str
    kind: str
    attempt_count: int
    max_attempts: int
    fencing_epoch: int
    idempotency_key: str
    priority: int
    due_at: datetime
    payload_version: int
    payload: Mapping[str, Any]
    lease_owner: str
    lease_until: datetime
    tenant_id: str | None = None
    operator_id: str | None = None
    character_id: str | None = None


@dataclass(frozen=True, slots=True)
class JobOutcome:
    """Result of a finished job — dry-run / validation payload stored in
    ``outcome_json`` for diagnostics. IDs / status only, never content."""

    detail: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class QueueStats:
    """Snapshot for observability (§12)."""

    status_counts: Mapping[str, int]
    kind_counts: Mapping[str, int]
    oldest_queued_age_seconds: float
    total: int


@runtime_checkable
class BackgroundJobQueuePort(Protocol):
    """Durable work queue with atomic claim, TTL leases and fencing."""

    async def enqueue(
        self, spec: BackgroundJobSpec, *, now: datetime,
    ) -> str | None:
        """Insert a job and return its id, or ``None`` when an active row
        with the same ``idempotency_key`` already exists, or when the
        coordinator lease is not currently held at ``spec.fencing_epoch``
        (a stale ex-coordinator, or an owner whose lease expired at
        ``now`` — idempotent / fenced, not an error). ``now`` is the
        caller's clock, used both for the lease-liveness check and for
        the row timestamps so the fencing decision and the persisted
        instants share one clock."""
        ...

    async def claim(
        self,
        worker_id: str,
        *,
        now: datetime,
        limit: int,
        lease_seconds: int,
        capability_caps: Mapping[str, int] | None = None,
    ) -> list[ClaimedJob]:
        """Atomically claim up to ``limit`` ready jobs (``queued`` +
        ``due_at <= now``, or a ``claimed`` job whose lease expired),
        ordered by ``priority`` then ``due_at``. Sets the lease and
        charges ``attempt_count``.

        ``capability_caps`` (§5) is an optional ``{capability: cap}`` map
        (e.g. ``{"llm": 3, "image": 1}``) that bounds how many jobs of a
        capped provider capability may be *in flight* (``claimed`` with a
        live lease) globally: a candidate whose kind maps to a capability
        already at its cap — counting live-claimed rows across all workers
        plus those selected earlier in this same batch — is skipped this
        pass and retried next loop. ``None`` (self-host / redrive-compat
        kinds) disables the filter. The count is over committed rows, so it
        shares the same benign READ-COMMITTED admission window as the
        per-tenant single-flight guard; a strict ceiling would additionally
        take a per-capability advisory lock (documented, not required at the
        hosted scale this caps)."""
        ...

    async def extend_lease(
        self, job_id: str, worker_id: str, *, until: datetime, now: datetime,
    ) -> bool:
        """Heartbeat: push ``lease_until`` out. ``False`` if the caller is
        not the current lease owner, the job is not ``claimed``, or the
        lease has ALREADY expired at ``now`` (``lease_until < now``) — an
        expired lease is not extendable by its former holder; it must be
        re-claimed (which charges an attempt), never silently revived."""
        ...

    async def complete(
        self,
        job_id: str,
        worker_id: str,
        *,
        outcome: JobOutcome,
        now: datetime,
    ) -> bool:
        """Mark ``done``. Only the lease owner with a still-valid lease
        succeeds; a stale caller gets ``False`` and no state change."""
        ...

    async def release_claim(
        self, job_id: str, worker_id: str, *, now: datetime,
    ) -> bool:
        """Hand an **unstarted** claim back to the queue (GD0 graceful stop).

        Undoes the claim exactly: back to ``queued`` at the same ``due_at``,
        lease cleared, and the attempt *refunded* — nothing ran, so charging it
        would let a rolling restart walk a job toward ``dead``. A surviving
        worker can then pick the job up immediately instead of waiting out the
        lease TTL.

        Only the current lease owner with a still-valid lease succeeds
        (``False`` otherwise, no state change): an expired lease may already
        have been re-claimed elsewhere, and requeueing it under that owner
        would resurrect a running job as ready work.

        Callers MUST only release work they have not begun; once a handler has
        applied any effect the job goes through ``complete`` / ``fail``."""
        ...

    async def fail(
        self,
        job_id: str,
        worker_id: str,
        *,
        error: BaseException | str,
        now: datetime,
        retry_in_seconds: int | None = None,
    ) -> bool:
        """Record a failure. With ``retry_in_seconds`` the job requeues at
        ``now + retry_in_seconds``; without it the job goes ``failed``.
        Either way, once ``attempt_count >= max_attempts`` the job goes
        ``dead`` regardless. ``False`` if the caller is not the valid
        lease owner."""
        ...

    async def stats(self, *, now: datetime) -> QueueStats: ...

    async def list_dead(self, *, limit: int) -> list[BackgroundJob]: ...

    async def redrive(self, job_id: str) -> bool:
        """Admin manual redrive (拍板): move a ``dead`` job back to
        ``queued`` with ``attempt_count`` reset and lease/error cleared.
        ``False`` if the job is missing or not ``dead``."""
        ...

    async def prune(self, *, now: datetime) -> int:
        """Delete terminal rows past retention (7d for done/failed/
        superseded, 30d for dead). Returns rows removed."""
        ...

    async def active_chain_keys(self) -> set[tuple[str, str]]:
        """``(kind, character_id)`` for every *active* (``queued`` /
        ``claimed``) character-scoped job (Phase 5 §4.2).

        The reconciler diffs this against the desired per-kind chains to
        detect a character that has no queued job for a kind (new / thawed /
        broken chain) and reseed it. Rows with a ``None`` ``character_id``
        (e.g. ``social_tick``) are excluded — they are not character chains."""
        ...

    async def dead_chain_keys(self) -> set[tuple[str, str]]:
        """``(kind, character_id)`` for every ``dead`` character-scoped job.

        A chain whose job exhausted ``max_attempts`` is a poison chain (拍板
        2026-07-20: ``dead`` rows get NO auto-retry, admin manual ``redrive``
        only). The reconciler unions these into the set it treats as "chain
        present" so it does NOT reseed a fresh occurrence every pass, which would
        be an unbounded auto-redrive loop defeating the dead-letter decision. An
        admin ``redrive`` moves the row back to ``queued`` (leaving the dead set),
        restoring reseed eligibility; a ``dead`` row also naturally drops out after
        its 30-day retention prune."""
        ...

    async def get(self, job_id: str) -> BackgroundJob | None: ...


@runtime_checkable
class BackgroundCoordinatorLeasePort(Protocol):
    """Single-leader runtime lease with a monotonic fencing epoch.

    The epoch is bumped ``+1`` on every ownership *change* and never
    reset. Re-acquiring / renewing as the same owner over a still-LIVE
    lease keeps the epoch so a coordinator's in-flight enqueues stay valid
    across renewals; re-acquiring over an EXPIRED lease is a new ownership
    incarnation and bumps the epoch even for the same owner (the old
    incarnation may be a partitioned zombie, so its epoch must be fenced
    out).
    """

    async def acquire(
        self, name: str, owner_id: str, *, ttl_seconds: int, now: datetime,
    ) -> int | None:
        """Acquire ``name``. Returns the epoch on success, or ``None``
        when a live lease is held by a different owner. A free/expired
        lease is an ownership change and bumps the epoch — INCLUDING a
        same-owner re-acquire over an expired lease (a new incarnation).
        Only a same-owner re-acquire over a still-live lease keeps the
        epoch."""
        ...

    async def renew(
        self, name: str, owner_id: str, *, ttl_seconds: int, now: datetime,
    ) -> int | None:
        """Extend the lease as the current owner. ``None`` when the caller
        is no longer the owner OR the lease has already expired
        (``lease_until < now``) — an expired lease cannot be renewed even
        by its original owner; it must be re-acquired (which bumps the
        epoch). The owner MUST stop on ``None``."""
        ...

    async def release(self, name: str, owner_id: str) -> bool: ...

    async def current(
        self, name: str,
    ) -> tuple[str, int, datetime] | None:
        """``(owner_id, epoch, lease_until)`` or ``None`` if never held."""
        ...


@runtime_checkable
class TickJournalPort(Protocol):
    """Append-only journal of scheduler tick buckets (shadow parity).

    Written by P2-B's shadow-gated embedded journal; the schema and port
    land now. ``kind`` is ``character_tick`` (with ``character_id``) or
    ``social_tick`` (``character_id`` is ``None``)."""

    async def record(
        self, entries: list[tuple[int, str, str | None]], *, now: datetime,
    ) -> None:
        """Append ``(tick_bucket, kind, character_id|None)`` rows."""
        ...

    async def list_range(
        self, *, from_bucket: int, to_bucket: int,
    ) -> list[tuple[int, str, str | None, datetime]]:
        """``(tick_bucket, kind, character_id, recorded_at)`` in range."""
        ...

    async def earliest_bucket_after(
        self, *, after_bucket: int, to_bucket: int,
    ) -> int | None:
        """Return the smallest bucket in ``(after_bucket, to_bucket]``.

        The query must inspect bucket values only; callers use this to avoid
        materialising a potentially huge sparse range before selecting the
        next journal batch.
        """
        ...

    async def list_bucket(
        self, bucket: int,
    ) -> list[tuple[int, str, str | None, datetime]]:
        """Return journal entries for exactly ``bucket``."""
        ...

    async def prune(self, *, now: datetime) -> int:
        """Delete entries older than 7 days. Returns rows removed."""
        ...


@runtime_checkable
class BackgroundCoordinatorCursorPort(Protocol):
    """Small durable key/value cursors for coordinator due-discovery
    bookmarks (§4.2). JSON payload of IDs / timestamps only."""

    async def get(self, name: str) -> Mapping[str, Any] | None: ...

    async def set(self, name: str, cursor: Mapping[str, Any]) -> None: ...

    async def advance_if_leader(
        self,
        cursor_name: str,
        candidate_bucket: int,
        lease_name: str,
        owner_id: str,
        epoch: int,
        now: datetime,
    ) -> bool:
        """Advance ``last_bucket`` iff the owner still holds a live epoch.

        The update is monotonic and preserves all other cursor JSON fields.
        """
        ...

    async def claim_due(
        self, name: str, owner_id: str = "", *, now: datetime,
        interval_seconds: float, reservation_seconds: float = 300.0,
    ) -> bool: ...

    async def complete_due(
        self, name: str, owner_id: str, *, completed_at: datetime,
    ) -> bool: ...

    async def release_due(self, name: str, owner_id: str) -> bool: ...
