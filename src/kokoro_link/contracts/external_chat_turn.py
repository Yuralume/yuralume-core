"""Ports and value objects for the external-chat turn receipt (LH2).

DR-LH0-004: a Core external-chat turn is a *recoverable state machine*, not a
single completed row written after the fact. The receipt is the durable spine::

    PROCESSING → GENERATED → COMMITTED → COMPLETED
                  ↘ FAILED_RETRYABLE / FAILED_TERMINAL

Invariants every adapter (in-memory + SQLAlchemy) MUST honour identically so
the shared contract tests hold:

* **Claim idempotency** — at most one receipt per
  ``(authenticated_caller, request_id)`` (DB ``UNIQUE``). ``claim_or_get`` is
  the single entry point: it inserts a fresh ``PROCESSING`` turn, or resolves
  the existing one to a replay / in-progress / recovery / conflict outcome.
* **Canonical-hash gate** — same id + same ``canonical_request_hash`` replays
  the stored outcome; same id + a *different* hash is a hard
  ``HashConflict`` (maps to ``409 idempotency_conflict``), never a silent
  overwrite.
* **Lease + fencing** — a claimed turn carries ``owner_id`` + a monotonic
  ``lease_version`` and a TTL ``lease_until``. EVERY mutation is a single
  conditional UPDATE fenced on ``owner_id`` + ``lease_version``; a stale owner
  (a lease that was taken over, bumping ``lease_version``) fails closed
  (returns ``False``). Lease liveness is judged by the DB clock
  (``CURRENT_TIMESTAMP``), never the caller's wall clock.
* **Recovery discipline** — an expired ``PROCESSING`` lease may be taken over
  and the LLM re-run (no durable ``GENERATED`` yet). An expired ``GENERATED``
  lease may ONLY be finalized from its stored snapshot — the LLM is NEVER
  re-run. ``COMMITTED`` only needs post-turn / replay. ``FAILED_TERMINAL`` is
  never taken over. ``FAILED_RETRYABLE`` is taken over on the SAME ``turn_id``.
* **Terminal immutability** — ``COMPLETED`` / ``FAILED_TERMINAL`` rows are
  frozen; a ``COMPLETED`` ``response_snapshot`` is never overwritten.

Snapshots (``generated_snapshot_json`` / ``response_snapshot_json`` /
``terminal_error_json``) are opaque JSON strings to this layer; their shape is
owned by the turn service.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ExternalChatTurnState(StrEnum):
    """Lifecycle of an :class:`ExternalChatTurnReceipt`."""

    PROCESSING = "PROCESSING"
    GENERATED = "GENERATED"
    COMMITTED = "COMMITTED"
    COMPLETED = "COMPLETED"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"


ACTIVE_STATES = frozenset(
    {
        ExternalChatTurnState.PROCESSING,
        ExternalChatTurnState.GENERATED,
        ExternalChatTurnState.COMMITTED,
    },
)
TERMINAL_STATES = frozenset(
    {
        ExternalChatTurnState.COMPLETED,
        ExternalChatTurnState.FAILED_TERMINAL,
    },
)


class PostTurnState(StrEnum):
    """Durable post-turn (memory / promise / schedule) intent status.

    ``COMMITTED`` transport completion does NOT claim all background extraction
    finished (DR-LH0-004); this tracks the stable-``turn_id`` post-turn job.
    """

    PENDING = "pending"
    ENQUEUED = "enqueued"
    DONE = "done"


CHANNEL_LINE = "line"


@dataclass(frozen=True, slots=True)
class ExternalChatTurnReceipt:
    """Full durable view of one external-chat turn."""

    turn_id: str
    authenticated_caller: str
    request_id: str
    contract_version: int | None
    canonical_hash_version: int | None
    canonical_request_hash: str
    tenant_id: str
    account_id: str
    character_id: str
    channel: str
    state: ExternalChatTurnState
    owner_id: str | None
    lease_until: datetime | None
    lease_version: int
    attempt_count: int
    conversation_id: str | None
    user_message_row_id: int | None
    assistant_message_row_id: int | None
    assistant_position: int | None
    generated_snapshot_json: str | None
    response_snapshot_json: str | None
    terminal_error_json: str | None
    post_turn_state: str
    post_turn_job_key: str | None
    last_error_code: str | None
    last_error_message: str | None
    created_at: datetime
    updated_at: datetime
    generated_at: datetime | None
    committed_at: datetime | None
    completed_at: datetime | None


# -- claim_or_get sealed-style result union --------------------------------- #
#
# Each variant carries the (possibly just-mutated) receipt so the caller has
# the current ``turn_id`` / ``owner_id`` / ``lease_version`` needed to fence
# every subsequent write.


@dataclass(frozen=True, slots=True)
class Claimed:
    """This caller owns the turn and must drive it (fresh claim, an expired
    ``PROCESSING`` takeover — re-run the LLM — or a ``FAILED_RETRYABLE``
    takeover on the same ``turn_id``). ``receipt`` carries the owner/lease to
    fence writes, plus any ``user_message_row_id`` / ``conversation_id`` already
    durable from a prior attempt."""

    receipt: ExternalChatTurnReceipt


@dataclass(frozen=True, slots=True)
class InProgress:
    """Another owner holds a live lease on this turn (``PROCESSING`` or
    ``GENERATED``). Maps to ``202`` — do not touch it."""

    receipt: ExternalChatTurnReceipt


@dataclass(frozen=True, slots=True)
class CompletedReplay:
    """The turn already reached ``COMPLETED``; replay the exact stored
    ``response_snapshot`` — never recompute."""

    receipt: ExternalChatTurnReceipt
    response_snapshot_json: str | None


@dataclass(frozen=True, slots=True)
class TerminalReplay:
    """The turn is ``FAILED_TERMINAL`` (e.g. ownership / character mismatch);
    replay the same stored terminal error. Never taken over."""

    receipt: ExternalChatTurnReceipt
    terminal_error_json: str | None


@dataclass(frozen=True, slots=True)
class CommittedRecovery:
    """The turn is ``COMMITTED`` (assistant durably appended) but never reached
    ``COMPLETED``. This caller has taken over the lease to finalize
    (post-turn + ``mark_completed``); it must NOT re-run the LLM."""

    receipt: ExternalChatTurnReceipt


@dataclass(frozen=True, slots=True)
class GeneratedRecovery:
    """A ``GENERATED`` turn whose lease expired. This caller has taken over to
    finalize FROM ``generated_snapshot`` — the assistant turn was already
    produced; the LLM must NEVER be re-run."""

    receipt: ExternalChatTurnReceipt


@dataclass(frozen=True, slots=True)
class HashConflict:
    """Same ``(authenticated_caller, request_id)`` but a different
    ``canonical_request_hash`` — a materially different request reusing an id.
    Maps to ``409 idempotency_conflict``."""

    receipt: ExternalChatTurnReceipt


ClaimOrGetResult = (
    Claimed
    | InProgress
    | CompletedReplay
    | TerminalReplay
    | CommittedRecovery
    | GeneratedRecovery
    | HashConflict
)


@runtime_checkable
class ExternalChatTurnReceiptPort(Protocol):
    """Durable receipt store for the recoverable external-chat turn machine.

    All lease timing is judged by the DB clock. Every write below is a single
    conditional UPDATE fenced on ``owner_id`` + ``lease_version`` and returns a
    ``bool`` (``True`` = the fenced owner still held the turn and the write
    applied; ``False`` = stale owner / wrong state / expired lease — no change).
    """

    async def claim_or_get(
        self,
        *,
        authenticated_caller: str,
        request_id: str,
        canonical_request_hash: str,
        canonical_hash_version: int | None,
        contract_version: int | None,
        tenant_id: str,
        account_id: str,
        character_id: str,
        requested_conversation_id: str | None = None,
    ) -> ClaimOrGetResult:
        """Insert a fresh ``PROCESSING`` turn, or resolve the existing one.

        Not present → insert (new ``turn_id`` / ``owner_id``,
        ``lease_version=1``, lease set) → :class:`Claimed`. Present with a
        different hash → :class:`HashConflict`. Otherwise dispatched by state
        and lease liveness (see module docstring): replay / in-progress /
        takeover. A takeover atomically bumps ``lease_version``, assigns a new
        ``owner_id``, charges ``attempt_count`` and extends the lease."""
        ...

    async def record_conversation(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        conversation_id: str,
    ) -> bool:
        """Persist the resolved (self-heal / created) ``source="line"``
        conversation id so a retry reuses it. From ``PROCESSING``."""
        ...

    async def record_user_turn(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        user_message_row_id: int,
    ) -> bool:
        """Persist the durably-appended user message row id (never rewritten on
        recovery). From ``PROCESSING``."""
        ...

    async def mark_generated(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        generated_snapshot_json: str,
    ) -> bool:
        """``PROCESSING`` → ``GENERATED``: durably checkpoint the assistant /
        response snapshot BEFORE the append so recovery finalizes without
        re-running the LLM."""
        ...

    async def mark_committed(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        assistant_message_row_id: int,
        assistant_position: int,
        response_snapshot_json: str,
    ) -> bool:
        """``PROCESSING`` / ``GENERATED`` → ``COMMITTED``: the assistant turn is
        durably appended exactly once; store its row id / position and the full
        response snapshot for replay."""
        ...

    async def mark_completed(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
    ) -> bool:
        """``COMMITTED`` → ``COMPLETED`` (terminal). The response snapshot is
        left untouched — it is never overwritten."""
        ...

    async def mark_failed(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
        retryable: bool,
        error_code: str,
        error_message: str,
        terminal_error_json: str | None = None,
    ) -> bool:
        """Record a failure. ``retryable`` → ``FAILED_RETRYABLE`` (takeover
        reuses the same ``turn_id``); otherwise ``FAILED_TERMINAL`` (frozen,
        replays ``terminal_error_json``). From ``PROCESSING`` / ``GENERATED``."""
        ...

    async def heartbeat_lease(
        self,
        *,
        turn_id: str,
        owner_id: str,
        lease_version: int,
    ) -> bool:
        """Push ``lease_until`` out while still the fenced owner of a live
        lease. ``False`` if the caller is not the owner, the lease already
        expired, or the turn is terminal — an expired lease is not extendable;
        it must be re-claimed via :meth:`claim_or_get`."""
        ...

    async def get(self, turn_id: str) -> ExternalChatTurnReceipt | None:
        """Full receipt view, or ``None``."""
        ...
