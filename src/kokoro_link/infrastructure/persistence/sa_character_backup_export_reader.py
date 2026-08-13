"""Read-only backup query layer for ``.lumebackup`` exports (CB2).

One deliberately *generic* reader instead of 30 new methods sprayed across
the existing repositories (plan §6「資料讀取走專用唯讀備份查詢層，不逐一
擴充既有 repository」): the CB0 registry already knows, per CARRY table,
which DTO travels and which columns stay home, so this module derives the
SELECT from that single source of truth —

- projected columns == the DTO's fields (never ``SELECT *`` — this is
  what keeps the two 1024-float pgvector columns of ``memory_items`` out
  of the dump, per the §3.2 column-level RECOMPUTE carve-out);
- the character predicate comes from the table's shape: ``characters``
  filters on ``id``, tables with their own ``character_id`` filter on it
  directly, and parent-linked tables (``messages``,
  ``schedule_activities``, ``story_arc_beats``, ``feed_reactions``,
  ``feed_comments``) resolve their FK column to the registered
  ``parent_table`` by metadata introspection and scope through an ``IN
  (SELECT id FROM parent WHERE character_id = :x)`` subquery — portable
  across SQLite (tests) and PostgreSQL (production);
- ``row_filter`` (today only ``story_seeds``'s ``character_id IS NOT
  NULL``) is applied verbatim on top.

Rows stream through ``session.stream`` so a years-old conversation table
never materialises as one list; ordering is by primary key so the same
database always produces the same archive (and tests can assert counts
deterministically). Each table dump uses its own short read session —
export runs against a live database and promises per-table consistency,
not a cross-table snapshot (same stance as every existing read surface).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.schema import Column, Table

from kokoro_link.application.dto.character_backup import (
    BackupClassification,
    BackupRecord,
    BackupTableRule,
    CharacterBackupRecord,
)
from kokoro_link.infrastructure.persistence.models import Base

_STREAM_BATCH_ROWS = 500


class SACharacterBackupExportReader:
    """Streams a character's CARRY rows straight off the schema."""

    def __init__(
        self, session_factory: sessionmaker[AsyncSession],
    ) -> None:
        self._session_factory = session_factory

    async def get_character_record(
        self, character_id: str, *, user_id: str,
    ) -> CharacterBackupRecord | None:
        """Owner-scoped character fetch — the export precheck.

        Cross-user access answers ``None`` exactly like a missing
        character (the API collapses both to 404, the enumeration-safe
        shape every ``/characters/{id}`` route already uses).
        """
        table = Base.metadata.tables["characters"]
        stmt = (
            _projected_select(table, CharacterBackupRecord)
            .where(table.c.id == character_id, table.c.user_id == user_id)
            .limit(1)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            row = result.first()
            return CharacterBackupRecord.from_row(row) if row is not None else None

    async def is_managed_character(self, character_id: str) -> bool:
        """Whether the character is a hosted *managed* (IP-partner)
        character (EC3 gate).

        Reads ``origin_official_card_id`` directly rather than through
        :meth:`get_character_record` — the CB0 registry deliberately
        excludes that column from :class:`CharacterBackupRecord` (it
        names a Cloud record a restore elsewhere could never resolve),
        but that exclusion governs what rides *inside* an archive, not
        whether an export may start at all. Callers are expected to have
        already confirmed the character exists and is theirs (via
        :meth:`get_character_record`); a since-deleted character simply
        answers ``False`` here rather than raising, since the caller's
        own not-found check already fired first in that race.
        """
        table = Base.metadata.tables["characters"]
        stmt = (
            select(table.c.origin_official_card_id)
            .where(table.c.id == character_id)
            .limit(1)
        )
        async with self._session_factory() as session:
            result = await session.execute(stmt)
            row = result.first()
            return bool(row is not None and row[0])

    async def stream_rows(
        self, rule: BackupTableRule, *, character_id: str,
    ) -> AsyncIterator[BackupRecord]:
        """Yield one DTO per row of ``rule.table`` belonging to the character."""
        if rule.classification is not BackupClassification.CARRY or rule.dto is None:
            raise ValueError(
                f"stream_rows only serves CARRY rules with a DTO, got {rule.table}",
            )
        table = Base.metadata.tables[rule.table]
        stmt = (
            _projected_select(table, rule.dto)
            .where(character_predicate(table, rule, character_id))
            .order_by(*table.primary_key.columns)
            .execution_options(yield_per=_STREAM_BATCH_ROWS)
        )
        if rule.row_filter:
            stmt = stmt.where(text(rule.row_filter))
        async with self._session_factory() as session:
            result = await session.stream(stmt)
            async for row in result:
                yield rule.dto.from_row(row)


def _projected_select(table: Table, dto: type[BackupRecord]) -> Select:
    """SELECT exactly the DTO's columns, in DTO field order."""
    return select(*(table.c[name] for name in dto.model_fields))


def character_predicate(
    table: Table, rule: BackupTableRule, character_id: str,
):
    """WHERE clause scoping ``rule.table`` to one character's rows.

    Public because the restore writer (CB3) uses the exact same predicate
    for its reverse-order failure cleanup — export scope and cleanup
    scope must be one definition or they will drift."""
    if rule.table == "characters":
        return table.c.id == character_id
    if "character_id" in table.c:
        return table.c.character_id == character_id
    if rule.parent_table is not None:
        parent = Base.metadata.tables[rule.parent_table]
        link = _parent_link_column(table, rule.parent_table)
        return link.in_(
            select(parent.c.id).where(parent.c.character_id == character_id),
        )
    raise LookupError(
        f"CARRY table {rule.table} has neither a character_id column nor "
        "a registered parent_table — the registry and the schema disagree",
    )


def _parent_link_column(table: Table, parent_table: str) -> Column:
    """The FK column of ``table`` that points at ``parent_table``'s PK."""
    for column in table.columns:
        for fk in column.foreign_keys:
            if fk.column.table.name == parent_table:
                return column
    raise LookupError(
        f"table {table.name} declares parent {parent_table} in the backup "
        "registry but carries no foreign key to it",
    )
