"""Durable pre-send ledger for external proactive delivery (LH4, DR-LH0-005).

Core saves an immutable ``event_id + envelope + payload_hash`` BEFORE it calls
the channel. If the response is lost, the same event is retried from the ledger
without re-running the proactive judge / decider (which would spend LLM budget
and could pick a different message). The channel de-duplicates on ``event_id``,
so a re-send of an already-accepted event is idempotent.

State machine (a delivery event, not a chat turn)::

    pending → accepted        (channel durably queued it)
            ↘ terminal        (permanently rejected — no endpoint / revoked)
            ↘ expired         (envelope ``expires_at`` passed before acceptance)

Invariants every adapter (in-memory + SQLAlchemy) MUST honour identically so
the shared contract suite holds:

* **Pre-send idempotency** — :meth:`save_pre_send` on the same ``event_id`` with
  the same ``payload_hash`` returns the existing row (a retry re-uses it); a
  DIFFERENT hash under the same id is a hard :class:`ExternalProactiveEventConflict`
  (a materially different event reusing an id), never a silent overwrite.
* **Immutable envelope** — ``envelope_json`` / ``payload_hash`` are written once
  at ``save_pre_send`` and never mutated; only ``state`` / ``accept_attempts`` /
  ``last_error`` / ``updated_at`` change afterwards.
* **Terminal-ish states are sticky** — ``mark_*`` transitions return ``bool``
  (``True`` when it applied). Marking a row that is already ``accepted`` /
  ``terminal`` / ``expired`` is a no-op that returns ``False``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol


class ExternalProactiveEventState(StrEnum):
    """Lifecycle of an :class:`ExternalProactiveEvent`."""

    PENDING = "pending"
    ACCEPTED = "accepted"
    TERMINAL = "terminal"
    EXPIRED = "expired"


class ExternalProactiveEventConflict(Exception):
    """Same ``event_id`` re-used with a different ``payload_hash``.

    The logical event materially changed under an id already committed to the
    ledger. Maps to a ``409 idempotency_conflict`` at the wire layer.
    """

    def __init__(self, event_id: str) -> None:
        super().__init__(
            f"external proactive event {event_id!r} already recorded with a "
            "different payload hash",
        )
        self.event_id = event_id


@dataclass(frozen=True, slots=True)
class ExternalProactiveEvent:
    """Durable view of one pre-send proactive delivery event."""

    event_id: str
    tenant_id: str
    account_id: str
    character_id: str
    kind: str
    envelope_json: str
    payload_hash: str
    state: ExternalProactiveEventState
    accept_attempts: int
    last_error: str | None
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    conversation_recorded: bool = False


class ExternalProactiveEventRepositoryPort(Protocol):
    """Durable store for the pre-send proactive delivery ledger."""

    async def save_pre_send(
        self,
        *,
        event_id: str,
        tenant_id: str,
        account_id: str,
        character_id: str,
        kind: str,
        envelope_json: str,
        payload_hash: str,
        expires_at: datetime,
        now: datetime,
    ) -> ExternalProactiveEvent:
        """Durably record the immutable event, or return the existing row.

        Fresh id → insert ``pending`` and return it. Existing id + same
        ``payload_hash`` → return the stored row (idempotent retry). Existing id
        + different ``payload_hash`` → raise :class:`ExternalProactiveEventConflict`.
        """
        ...

    async def mark_accepted(
        self, event_id: str, *, now: datetime,
    ) -> bool:
        """``pending`` → ``accepted`` (charges ``accept_attempts``). ``False``
        if the row is missing or no longer pending."""
        ...

    async def mark_terminal(
        self, event_id: str, *, reason: str, now: datetime,
    ) -> bool:
        """``pending`` → ``terminal`` with a structured ``reason``. ``False`` if
        the row is missing or no longer pending."""
        ...

    async def mark_expired(
        self, event_id: str, *, now: datetime,
    ) -> bool:
        """``pending`` → ``expired`` (envelope validity window passed before
        acceptance). ``False`` if the row is missing or no longer pending."""
        ...

    async def list_pending_due(
        self, *, now: datetime, limit: int = 100,
    ) -> list[ExternalProactiveEvent]:
        """Pending events still within their validity window
        (``expires_at > now``), oldest-created first — the retry worklist for a
        lost response. Expired-past-``now`` rows are excluded so a sweeper can
        move them to ``expired`` instead of re-sending a stale message."""
        ...

    async def mark_conversation_recorded(
        self, event_id: str, *, now: datetime,
    ) -> bool:
        """``accepted`` + ``conversation_recorded=False`` → ``conversation_recorded=True``
        (M8). Idempotent: ``False`` when the row is missing, not accepted, or
        already recorded. Marks that the delivered proactive turn was durably
        appended to the character's conversation history."""
        ...

    async def list_accepted_unrecorded(
        self, *, now: datetime, limit: int = 100,
    ) -> list[ExternalProactiveEvent]:
        """Accepted events whose conversation history append has NOT yet
        succeeded (``state='accepted'`` and ``conversation_recorded=False``),
        oldest-created first (M8) — the retry worklist for a lost / crashed
        history write so the accepted send and the conversation row converge."""
        ...

    async def get(self, event_id: str) -> ExternalProactiveEvent | None:
        """Full event view, or ``None``."""
        ...
