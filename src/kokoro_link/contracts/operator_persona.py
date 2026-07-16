"""``OperatorPersonaRepositoryPort`` — persistence boundary for the
five-layer operator persona (extracted facts about the operator).

Distinct from :mod:`operator_profile`: that one stores the operator's
self-declared name/aliases/pronouns; **this** one stores what the
characters have learned during conversation. The two never collide —
each layer-1 ``name`` field here is one observation, not the
authoritative display name. The dream job is responsible for syncing
a high-confidence layer-1 ``name`` back to ``OperatorProfile`` when
that escalation is warranted.

Repositories return ``ProfileField`` (with ``field_id`` populated) and
``CandidateField`` (with ``candidate_id`` populated). Both staging and
confirmed rows share a single table; the ``state`` column distinguishes
them. The port hides that detail from callers.
"""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    ProfileField,
)


class OperatorPersonaRepositoryPort(Protocol):
    async def get(
        self, character_id: str, operator_id: str,
    ) -> OperatorPersona:
        """Load the persona aggregate for ``(character_id, operator_id)``.

        Always returns a value — never-seen pairs come back with empty
        layer dicts and ``layer4_interaction`` is ``None`` (the
        application service fills it via the strength calculator).
        Pending candidates are loaded too so a dream pass can inspect
        them without a second round trip.
        """

    async def upsert_field(
        self, character_id: str, operator_id: str, field: ProfileField,
    ) -> ProfileField:
        """Insert or update a confirmed field. Returns the persisted
        instance with ``field_id`` populated.

        Uniqueness is keyed on ``(character_id, operator_id, layer,
        field_key, state)`` so a confirmed row coexists with any
        pending shadows of the same key; the dream job removes /
        supersedes shadows explicitly via :meth:`mark_state`.
        """

    async def upsert_candidate(
        self,
        character_id: str,
        operator_id: str,
        candidate: CandidateField,
    ) -> CandidateField:
        """Stage a candidate (state defaults to ``pending``). Returns
        the persisted instance with ``candidate_id`` populated.

        Idempotency note: the extraction pass may produce the same
        (field_key, value, evidence quote) twice in a row if the user
        repeats themselves. Implementations should de-duplicate on
        ``(character_id, operator_id, layer, field_key, state,
        evidence quote)`` to avoid runaway staging rows.
        """

    async def list_pending(
        self,
        character_id: str,
        operator_id: str,
        *,
        limit: int = 100,
    ) -> list[CandidateField]:
        """Return staged candidates oldest first — dream job consumes
        them FIFO. ``limit`` caps the batch handed to a single LLM
        consolidation call."""

    async def count_pending(
        self, character_id: str, operator_id: str,
    ) -> int:
        """Cheap count used by the dream-trigger gate.

        ``PersonaDreamService.should_run_now`` checks this before
        spinning up an LLM call.
        """

    async def list_confirmed_for_decay(
        self,
        character_id: str,
        operator_id: str,
        *,
        stale_after_days: int,
    ) -> list[ProfileField]:
        """Return confirmed fields whose ``last_updated`` is older than
        ``stale_after_days`` and which are eligible for the dream
        decay pass. Cheaper than loading the full persona just to
        decide what to age out."""

    async def list_characters_with_pending(self) -> list[tuple[str, str]]:
        """Return ``(character_id, operator_id)`` pairs that have at
        least one pending candidate. The proactive scheduler uses this
        to fan-out dream ticks to only the pairs that actually have
        work — avoids spinning up an LLM call per character on every
        tick when nothing's staged."""

    async def get_row_scope(self, row_id: str) -> tuple[str, str] | None:
        """Return ``(character_id, operator_id)`` for a persona row by its
        id, or ``None`` if no such row exists.

        Works for both confirmed fields and pending candidates — they
        share one table and one id space. Ownership guards call this
        before a single-row mutation (``mark_state`` / ``mark_field_state``)
        so a caller can't transition a row that belongs to a different
        operator just by knowing its id. The mutation methods stay
        scope-agnostic (the dream job already operates within a resolved
        ``(character, operator)`` pair); this is the read used to enforce
        the boundary at the API edge."""

    async def mark_state(self, candidate_id: str, state: str) -> None:
        """Transition a candidate to a terminal state (``promoted`` /
        ``rejected`` / ``stale`` / ``superseded``). The actual
        confirmed field is inserted separately via
        :meth:`upsert_field`; this just stamps the staging row so the
        next dream pass doesn't reconsider it."""

    async def mark_field_state(self, field_id: str, state: str) -> None:
        """Same as :meth:`mark_state` but for confirmed rows — used
        when a supersede or decay pass retires an existing field."""

    async def delete_for_character(self, character_id: str) -> int:
        """Delete all persona rows owned by one character.

        Used by the character reset / delete paths. Returns the number
        of rows affected.
        """

    async def reject_evidence_since(
        self,
        *,
        conversation_id: str,
        since,
    ) -> int:
        """Mark rows backed by evidence from ``conversation_id`` after
        ``since`` as ``rejected``.

        Undo uses this to roll back persona extraction generated by the
        reverted turn. Implementations may scan JSON evidence if the
        storage shape is denormalised.
        """
