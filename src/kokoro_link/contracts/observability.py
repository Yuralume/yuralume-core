"""Ports for turn recording / replay / dashboard observability.

Two roles, deliberately split:

* ``TurnRecorderPort`` — write-only side effect. Application services
  (ChatService, ProactiveDispatcher, idle drift, dream, planner) call
  ``record(...)`` from the *foreground* path; the adapter is expected
  to fire-and-forget so the turn isn't blocked by audit IO.
* ``TurnRecordRepositoryPort`` — read access for the replay CLI and the
  observability dashboard.

We keep them as separate Protocols because the foreground path should
not have type access to query methods (no temptation to read-modify-
write).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from kokoro_link.domain.entities.turn_record import TurnKind, TurnRecord


@dataclass(frozen=True, slots=True)
class TurnRecordingDraft:
    """Inputs the application layer hands to the recorder.

    The recorder is responsible for stamping ``created_at`` and
    persisting. Callers may pass a preallocated ``id`` when downstream
    side effects need to reference the turn before the background write
    lands.
    """
    character_id: str
    kind: TurnKind
    id: str | None = None
    model_id: str = ""
    prompt_pack_hash: str = ""
    prompt_assembled: str = ""
    response_text: str = ""
    conversation_id: str | None = None
    response_json: dict[str, Any] | None = None
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None
    post_turn_refs: dict[str, Any] | None = None


class TurnRecorderPort(Protocol):
    async def record(self, draft: TurnRecordingDraft) -> str:
        """Persist the turn record. Returns the assigned ``TurnRecord.id``.

        Implementations should be fire-and-forget where possible — the
        caller awaits the coroutine but the persistence write may be
        scheduled on a background task. Failures must not bubble up to
        the caller; log and swallow.
        """
        ...


@dataclass(frozen=True, slots=True)
class FunnelBucket:
    """One row of the proactive-funnel aggregate response."""
    kind: TurnKind
    count: int


@dataclass(frozen=True, slots=True)
class LatencyBucket:
    """One bucket of the latency histogram."""
    lower_ms: int
    upper_ms: int | None
    """``None`` means open-ended (the last bucket)."""
    count: int


class TurnRecordRepositoryPort(Protocol):
    async def add(self, record: TurnRecord) -> None: ...

    async def get(self, record_id: str) -> TurnRecord | None: ...

    async def list_recent(
        self,
        *,
        character_id: str | None = None,
        kind: TurnKind | None = None,
        since: datetime | None = None,
        operator_feedback_kind: str | None = None,
        exclude_content_mode: str | None = None,
        limit: int = 50,
    ) -> list[TurnRecord]: ...

    async def update_operator_feedback(
        self,
        record_id: str,
        feedback: dict[str, Any],
    ) -> TurnRecord | None: ...

    async def latency_histogram(
        self,
        *,
        character_id: str | None = None,
        kind: TurnKind | None = None,
        since: datetime | None = None,
        buckets_ms: tuple[int, ...] = (50, 200, 500, 1000, 3000),
    ) -> list[LatencyBucket]: ...
