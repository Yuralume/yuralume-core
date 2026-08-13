"""Write-side twin of the backup export reader (CB3).

The restore lands rows with plain SQLAlchemy Core inserts derived from
the CB0 registry — mirroring the exporter's deliberate stance (plan §6:
專用查詢層，不逐一擴充既有 repository): the import service owns the D4/D5
semantics (id remapping, operator rebinding, NSFW folding) and hands this
module fully rewritten column dicts; this module owns everything that
requires schema introspection —

- **batched inserts** in registry CARRY order, with SQLAlchemy column
  defaults filling the columns the DTOs deliberately don't carry
  (B-layer carve-outs of ``characters``, the RECOMPUTE vector columns of
  ``memory_items``),
- **unique-constraint dedup**: a restore rebinds every operator-scoped
  row to the importer, so a backup exported from a multi-operator
  deployment can produce rows that collide on ``(character_id,
  operator_id, …)`` uniques. Colliding rows are skipped (first wins),
  never allowed to poison the insert transaction. NULL-containing
  tuples never dedup — SQL unique semantics,
- **foreign-key facts** (:meth:`reference_columns`) so the service can
  apply its remap rules per referencing column without restating the
  schema,
- **reverse-order cleanup** (:meth:`delete_all_for_character`) using the
  exact export-scope predicate, so「失敗清理」and the §3 boundary can
  never drift apart.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import UniqueConstraint, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.schema import Table

from kokoro_link.application.dto.character_backup import (
    carried_table_rules,
)
from kokoro_link.infrastructure.persistence.models import Base
from kokoro_link.infrastructure.persistence.sa_character_backup_export_reader import (
    character_predicate,
)


@dataclass(frozen=True, slots=True)
class ForeignRef:
    """One foreign-key column of a CARRY table, as remap input."""

    referenced_table: str
    nullable: bool


@dataclass
class _UniqueGuard:
    """Per-table seen-tuples over PK + unique constraints."""

    column_sets: tuple[tuple[str, ...], ...]
    seen: list[set[tuple]] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.seen = [set() for _ in self.column_sets]

    def admit(self, row: dict) -> bool:
        """Record ``row``; ``False`` when it collides with an earlier one.

        Tuples containing ``None`` never collide (SQL NULL semantics),
        and sets whose columns aren't all present in the row (e.g. an
        autoincrement PK the caller omitted) are skipped.
        """
        keys: list[tuple[int, tuple]] = []
        for index, columns in enumerate(self.column_sets):
            if any(name not in row for name in columns):
                continue
            values = tuple(row[name] for name in columns)
            if any(value is None for value in values):
                continue
            if values in self.seen[index]:
                return False
            keys.append((index, values))
        for index, values in keys:
            self.seen[index].add(values)
        return True


class SACharacterBackupRestoreWriter:
    """Stateless factory — :meth:`start` opens one restore's landing."""

    def __init__(
        self, session_factory: sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    def start(self, character_id: str) -> "CharacterRestoreLanding":
        return CharacterRestoreLanding(
            self._session_factory, character_id=character_id,
        )

    async def set_character_frozen_state(
        self,
        character_id: str,
        *,
        frozen: bool,
        frozen_at: datetime | None,
        frozen_reason: str | None,
    ) -> None:
        """Write the character's freeze columns directly (A3 finalize).

        The landing pass lands the ``characters`` row frozen under the
        ``restore`` provenance; when the restore succeeds, the import
        service writes the exported freeze state back through this seam —
        a targeted UPDATE rather than a repository round-trip, because
        the domain repository's ``set_frozen`` clears/stamps provenance
        with its own semantics and this write must restore the exported
        columns verbatim."""
        table = Base.metadata.tables["characters"]
        async with self._session_factory() as session:
            await session.execute(
                update(table)
                .where(table.c.id == character_id)
                .values(
                    frozen=frozen,
                    frozen_at=frozen_at,
                    frozen_reason=frozen_reason,
                ),
            )
            await session.commit()

    async def delete_all_for_character(self, character_id: str) -> None:
        """Reverse-CARRY delete of everything the boundary scopes to
        ``character_id`` — the failure / crash-recovery cleanup. Children
        go first so parent-linked predicates still resolve and the
        ``story_events → story_seeds`` RESTRICT is honoured; the
        ``characters`` row (first CARRY rule) goes last."""
        async with self._session_factory() as session:
            for rule in reversed(carried_table_rules()):
                table = Base.metadata.tables[rule.table]
                await session.execute(
                    delete(table).where(
                        character_predicate(table, rule, character_id),
                    ),
                )
            await session.commit()


class CharacterRestoreLanding:
    """One restore run's write handle (dedup state lives here)."""

    def __init__(
        self,
        session_factory: sessionmaker[AsyncSession],
        *,
        character_id: str,
    ) -> None:
        self._session_factory = session_factory
        self._character_id = character_id
        self._guards: dict[str, _UniqueGuard] = {}

    # -- schema facts ---------------------------------------------------

    def reference_columns(self, table_name: str) -> dict[str, ForeignRef]:
        """FK columns of ``table_name`` → what they point at."""
        table = Base.metadata.tables[table_name]
        refs: dict[str, ForeignRef] = {}
        for column in table.columns:
            for fk in column.foreign_keys:
                refs[column.name] = ForeignRef(
                    referenced_table=fk.column.table.name,
                    nullable=bool(column.nullable),
                )
        return refs

    # -- queries --------------------------------------------------------

    async def existing_ids(
        self, table_name: str, ids: list[str],
    ) -> set[str]:
        """Which of ``ids`` already exist as PKs on this instance —
        the fail-soft check for provenance refs into shared pools
        (global story seeds, pack arc series)."""
        if not ids:
            return set()
        table = Base.metadata.tables[table_name]
        (pk_column,) = table.primary_key.columns
        async with self._session_factory() as session:
            result = await session.execute(
                select(pk_column).where(pk_column.in_(ids)),
            )
            return {value for (value,) in result.all()}

    # -- writes ---------------------------------------------------------

    async def insert_rows(
        self, table_name: str, rows: list[dict],
    ) -> tuple[int, int]:
        """Insert ``rows`` (already rewritten); returns
        ``(inserted, skipped_duplicates)``."""
        if not rows:
            return 0, 0
        guard = self._guards.get(table_name)
        if guard is None:
            guard = _UniqueGuard(
                _unique_column_sets(Base.metadata.tables[table_name]),
            )
            self._guards[table_name] = guard
        admitted = [row for row in rows if guard.admit(row)]
        skipped = len(rows) - len(admitted)
        if not admitted:
            return 0, skipped
        table = Base.metadata.tables[table_name]
        async with self._session_factory() as session:
            await session.execute(insert(table), admitted)
            await session.commit()
        return len(admitted), skipped


def _unique_column_sets(table: Table) -> tuple[tuple[str, ...], ...]:
    sets: list[tuple[str, ...]] = [
        tuple(column.name for column in table.primary_key.columns),
    ]
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            sets.append(
                tuple(column.name for column in constraint.columns),
            )
    for column in table.columns:
        if column.unique:
            sets.append((column.name,))
    # Partial (WHERE-qualified) unique indexes are intentionally NOT
    # collected: their predicate isn't evaluated here, and treating them
    # as full uniques would wrongly drop legitimate rows.
    return tuple(dict.fromkeys(sets))
