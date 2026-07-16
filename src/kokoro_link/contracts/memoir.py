"""Memoir pin repository port.

Storage-agnostic interface for player-side memoir pins. The pin table is
the only memoir-specific persistence — chapters and timeline entries are
projected on-the-fly from the existing memory / reflection / emotion
repositories.

Per ``docs/MEMOIR_PLAN.md`` repositories MUST enforce per-(character_id,
operator_id) isolation; the SA implementation does this via a unique
constraint, the in-memory implementation by composite-key filtering.
"""

from __future__ import annotations

from typing import Protocol

from kokoro_link.domain.entities.memoir_pin import MemoirPin


class MemoirPinRepositoryPort(Protocol):
    async def list_for(
        self, character_id: str, operator_id: str,
    ) -> list[MemoirPin]:
        """Return every pin owned by ``(character_id, operator_id)``,
        newest pin first.

        Cross-operator pins MUST NOT leak into the result. Empty list
        when the pair has never pinned anything."""

    async def add(self, pin: MemoirPin) -> MemoirPin:
        """Insert ``pin``. Idempotent: re-pinning an already-pinned
        ``(character_id, operator_id, entry_kind, entry_id)`` returns
        the existing row unchanged (no duplicate, no error).

        Implementations are responsible for honouring the
        ``MemoirSettings.pin_max_per_pair`` ceiling **before** writing —
        the service calls :meth:`count_for` and rejects with a service-
        layer error *before* invoking ``add``, so the repo can stay
        focused on storage."""

    async def remove(
        self,
        character_id: str,
        operator_id: str,
        entry_kind: str,
        entry_id: str,
    ) -> bool:
        """Delete the matching pin. Returns ``True`` if a row was
        removed, ``False`` if no matching pin existed (allows the API
        layer to map "remove nothing" to 404)."""

    async def count_for(
        self, character_id: str, operator_id: str,
    ) -> int:
        """Return the current pin count for the pair. Used by the service
        to enforce :attr:`MemoirSettings.pin_max_per_pair`."""

    async def delete_for_character(self, character_id: str) -> int:
        """Cascade hook: wipe every pin for a character (used when the
        character itself is deleted or factory-reset). Returns the
        number of rows removed."""
