"""角色資料邊界的**重置引擎**（CD3, ROADMAP #35）.

``reset_character_data`` used to carry its own private
``flag -> repository`` if/elif ladder inside ``character_service.py`` —
four blocks that were really one fact (which table each flag clears)
written out four times. That fact now lives in exactly one place, CD0's
``consumer_policies.RESET_FLAG_EFFECTS``:

* **which tables a flag touches** — resolved through
  :func:`~kokoro_link.application.dto.character_backup.consumer_policies.reset_effects_for_flag`,
* **which table's row count is reported** — resolved through
  :func:`~kokoro_link.application.dto.character_backup.consumer_policies.reset_primary_table_for_flag`,
* **how to execute it** — here (the port + the in-memory fallback) plus
  the SA adapter (:mod:`kokoro_link.infrastructure.persistence.sa_character_reset_eraser`).

This module holds no table list of its own. A flag's reach changing in
``consumer_policies.py`` changes what both erasers do without either one
being edited.

**Semantics carried over unchanged from pre-CD3** (CD0 already verified
and pinned these; this ticket does not revisit them):

* Each flag reports the row count of its *primary* table only —
  ``conversations=True`` reports the ``conversations`` table's count,
  not the ``messages`` / ``turn_journals`` / ``pending_follow_ups`` /
  ``story_scene_sessions`` rows the same reset also deletes through the
  conversation parent chain, nor the ``channel_bindings`` row whose
  pointer it nulls.
* ``PURGE_VIA_PARENT`` and ``CLEAR_REFERENCE`` tables are **not
  reported**, but they are still **acted on**: the SQL eraser emits an
  explicit ``DELETE`` / ``UPDATE … SET NULL`` for every such effect. The
  FK's ``ON DELETE`` verb is the schema layer *agreeing* with the policy
  — it is what makes the derived statement the correct one (it supplies
  the parent chain the statement filters through), never the mechanism
  the reset leans on. Self-host SQLite runs without
  ``PRAGMA foreign_keys=ON`` (``persistence/engine.py`` never sets it),
  so an eraser that relied on the cascade would delete **nothing** at
  all there. See
  :attr:`~kokoro_link.application.dto.character_backup.consumer_policies.ResetPolicy.PURGE_VIA_PARENT`
  and :mod:`kokoro_link.infrastructure.persistence.sa_character_reset_eraser`.
* The character row itself is never touched by reset; only
  ``delete_character`` removes it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from kokoro_link.application.dto.character_backup.consumer_policies import (
    ResetFlag,
)

CharacterResetCounts = Mapping[ResetFlag, int]
"""One row count per requested flag — the primary-table count, see the
module docstring. Flags not passed to :meth:`CharacterResetEraserPort.erase`
are simply absent (never zero-by-omission vs zero-because-empty
ambiguity: the caller only asks for what it requested)."""


class SupportsCharacterScopedDelete(Protocol):
    """The one method every character-scoped repository already has."""

    async def delete_for_character(self, character_id: str) -> int:
        ...


class CharacterResetEraserPort(Protocol):
    """The execution half of ``reset_character_data`` — one implementation
    per persistence mode, mirroring :class:`CharacterDatabaseEraserPort`
    from the CD2 delete engine."""

    async def erase(
        self, character_id: str, flags: frozenset[ResetFlag],
    ) -> CharacterResetCounts:
        """Clear every table the requested flags declare, return each
        flag's primary-table row count. ``flags`` empty is a no-op."""


class RepositoryCharacterResetEraser:
    """Erasure through the repository ports — the DB-less container mode.

    ``repositories`` maps each flag to the one repository whose
    ``delete_for_character`` already implements that flag's primary-table
    purge. How far past that primary table a given repository reaches is
    its own business — this class only calls the one method every
    character-scoped repository exposes, and never assumes a database
    cascade cleans up behind it. **This mode is therefore narrower than
    the RESET policy's declared boundary**: the ``PURGE_VIA_PARENT`` /
    ``CLEAR_REFERENCE`` effects that :class:`SACharacterResetEraser`
    states explicitly have no repository to state them here. Acceptable
    for the same reason as
    :class:`~kokoro_link.application.services.character_data_erasure.RepositoryCharacterDatabaseEraser`:
    this is the DB-less development / test fallback, and the boundary is
    pinned against the SA path every real deployment runs.

    A flag with no repository wired reports zero rather than raising,
    matching pre-CD3 behaviour where an unwired repository silently left
    that flag's count at zero.
    """

    def __init__(
        self,
        repositories: Mapping[ResetFlag, SupportsCharacterScopedDelete | None],
    ) -> None:
        self._repositories = dict(repositories)

    async def erase(
        self, character_id: str, flags: frozenset[ResetFlag],
    ) -> CharacterResetCounts:
        counts: dict[ResetFlag, int] = {}
        for flag in flags:
            repository = self._repositories.get(flag)
            if repository is None:
                counts[flag] = 0
                continue
            deleted = await repository.delete_for_character(character_id)
            counts[flag] = int(deleted or 0)
        return counts


class FlagGatedCharacterResetEraser:
    """Drops flags whose subsystem this deployment did not wire.

    Pre-CD3, ``reset_character_data`` called one repository per flag, so
    a subsystem that was switched off (``KOKORO_PERSONA_ENABLED=false``
    leaves ``operator_persona_repository`` unbuilt) made that flag a
    silent no-op reporting zero. The SQL reset eraser has no such
    coupling — it derives its tables from the registry and would happily
    hard-delete ``operator_profile_fields`` on a deployment where the
    persona feature does not exist, which is a behaviour change CD3 is
    not allowed to make.

    So availability is re-attached here, in front of *both* persistence
    modes, rather than inside either one: the SQL eraser stays a pure
    registry derivation, and the two container modes answer identically.
    A dropped flag reports ``0`` — never absent, or the caller could not
    tell "switched off" from "nothing to delete", and pre-CD3 reported
    zero.
    """

    def __init__(
        self,
        inner: CharacterResetEraserPort,
        *,
        available_flags: frozenset[ResetFlag],
    ) -> None:
        self._inner = inner
        self._available_flags = available_flags

    async def erase(
        self, character_id: str, flags: frozenset[ResetFlag],
    ) -> CharacterResetCounts:
        allowed = flags & self._available_flags
        counts = (
            await self._inner.erase(character_id, allowed) if allowed else {}
        )
        return {flag: counts.get(flag, 0) for flag in flags}
