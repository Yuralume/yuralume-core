"""Pending-follow-up repository port.

Storage-agnostic CRUD for the deferred-reply queue. In-memory adapter is
used by unit tests; the SQLAlchemy adapter persists in the
``pending_follow_ups`` table.

Lookups the service / dispatcher need:

* ``find_open_for_conversation(...)`` — when a new user message lands,
  see if there's already an open row to merge into (the *merge-don't-
  cancel* policy).
* ``list_due(...)`` — the proactive-scheduler tick walks rows whose
  ``scheduled_for <= now`` and ``status == queued`` (or ``resolving``
  left dangling by an earlier crash), grouped by character so the
  dispatcher can apply per-character gating.

Cascade helpers mirror the journal / arc / schedule shape so character
deletion stays atomic.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from kokoro_link.domain.entities.pending_follow_up import PendingFollowUp


class PendingFollowUpRepositoryPort(Protocol):
    async def add(self, follow_up: PendingFollowUp) -> None:
        """Persist a new row."""

    async def save(self, follow_up: PendingFollowUp) -> None:
        """Upsert. Used when status / messages mutate."""

    async def get(self, follow_up_id: str) -> PendingFollowUp | None:
        """Fetch a single row by id."""

    async def find_open_for_conversation(
        self, conversation_id: str,
    ) -> PendingFollowUp | None:
        """Return the open (``queued`` or ``resolving``) row for the
        conversation, if any. Used by ``ChatService`` to decide whether
        to merge a new user message into an existing pending row.

        Only one row per conversation may be open at a time — the
        merge policy collapses everything into a single record. The
        repository enforces this implicitly via the queue/resolve flow
        but does **not** add a unique constraint (a stale resolving row
        from a crashed dispatcher should not block a new defer).
        """

    async def list_due(
        self,
        *,
        now: datetime,
        limit: int = 50,
    ) -> list[PendingFollowUp]:
        """Return queued rows whose ``scheduled_for <= now``.

        Ordered by ``scheduled_for`` ascending (FIFO) and capped at
        ``limit`` so a backlog can't starve other tick work. The
        dispatcher applies per-character busy / energy filtering before
        actually firing — this port is a coarse cursor.
        """

    async def list_open_for_character(
        self, character_id: str,
    ) -> list[PendingFollowUp]:
        """All open rows belonging to ``character_id``. Used by tests
        and by the cascading delete flow."""

    async def delete_for_conversation(self, conversation_id: str) -> int:
        """Cascade-delete every row tied to a conversation."""

    async def delete_for_character(self, character_id: str) -> int:
        """Cascade-delete every row belonging to ``character_id``.

        Called from ``CharacterService.delete_character`` so deferred
        replies don't outlive their owner.
        """
