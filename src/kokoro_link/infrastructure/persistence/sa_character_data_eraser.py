"""Registry-driven SQL erasure of one character's boundary (CD2).

Twin of :mod:`sa_character_backup_restore_writer`'s
``delete_all_for_character``, widened from "the CARRY tables a failed
restore left behind" to "everything the DELETE policy purges" — the
runtime/audit ledgers and the cross-character tables included. Both walk
the registry in reverse declaration order for the same reason: the
registry declares parents before children, so reversing it deletes
children first, honours the ``story_events → story_seeds`` RESTRICT, and
leaves the ``characters`` row (rule #0) for last.

What this module knows that the registry cannot express:

* **``PURGE_ANY_SIDE`` needs a two-sided predicate.** The four
  cross-character tables carry two character columns and — unlike every
  other character-referencing table — **no foreign key to
  ``characters``** at all, so the predicate is derived from column
  *names* (``character_id`` / ``peer_character_id`` /
  ``from_character_id`` / ``to_character_id`` / ``character_a_id`` /
  ``character_b_id``). Deleting only the owning side would leave the peer
  holding half a relationship.
* **``PURGE_VIA_PARENT`` needs a parent-scoped subquery.** Those tables
  have no character column of their own; their rows are defined by the
  parent's ``ON DELETE CASCADE``. The eraser used to emit *nothing* for
  them and let the database do it — but that only holds where foreign
  keys are actually enforced: PostgreSQL always, SQLite only under
  ``PRAGMA foreign_keys=ON``, which
  :mod:`kokoro_link.infrastructure.persistence.engine` never sets. So on
  every self-host SQLite deployment those children silently survived the
  delete. Now the statement is explicit, derived in
  :mod:`sa_character_parent_links` from the FK the policy names, and the
  zero-orphan guarantee no longer depends on a database-level setting.
  The pin test runs both with and without the pragma for that reason.
* **``RETAIN`` / ``SKIP`` are skipped by policy, never by table name.**
  A hard-coded table list here would be a second boundary definition, and
  the one thing CD0 exists to prevent is a second boundary definition.

The media harvest reads the registry's URL columns through the same
per-table character predicate the exporter uses, so "which media belongs
to this character" cannot drift between backup and delete.
"""

from __future__ import annotations

import re
from functools import cache
from types import MappingProxyType

from sqlalchemy import delete, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.schema import Column, Table

from kokoro_link.application.dto.character_backup import (
    BACKUP_TABLE_RULES_BY_NAME,
    CHARACTER_BACKUP_TABLE_RULES,
    DataConsumer,
    DeletePolicy,
    media_url_fields_by_table,
    policy_for,
)
from kokoro_link.application.services.character_data_erasure import (
    DatabaseErasureResult,
    media_urls_from_value,
)
from kokoro_link.infrastructure.persistence.models import Base
from kokoro_link.infrastructure.persistence.sa_character_backup_export_reader import (
    character_predicate,
)
from kokoro_link.infrastructure.persistence.sa_character_parent_links import (
    CASCADE,
    parent_link_predicate,
)

_CHARACTER_REF_COLUMN = re.compile(r"character.*_id$")
"""Scalar character-reference column names. Same shape the CB0 registry
pin test scans for, minus the ``*_ids_json`` multi-character columns —
those belong to the co-authored works the D2 ruling keeps intact, and are
never a delete predicate."""

_STREAM_BATCH_ROWS = 500
"""Same yield size the backup export reader streams at. The harvest walks
``messages`` — the one table that is routinely six figures — so it must
not materialise its result set."""

_EMITTING_POLICIES = (
    DeletePolicy.PURGE,
    DeletePolicy.PURGE_ANY_SIDE,
    DeletePolicy.PURGE_VIA_PARENT,
)

_DIRECTLY_PURGED_POLICIES = (DeletePolicy.PURGE, DeletePolicy.PURGE_ANY_SIDE)


@cache
def directly_purged_tables() -> frozenset[str]:
    """Tables whose rows this delete removes by their own predicate.

    The set a ``PURGE_VIA_PARENT`` child is allowed to hang off: a
    cascade only takes the child when the parent is genuinely going.
    Derived from the registry so it cannot drift from the policy, and
    resolved lazily — the registry is static, but an unresolvable policy
    should surface as a failing delete, not as a container that refuses
    to import.
    """
    return frozenset(
        rule.table
        for rule in CHARACTER_BACKUP_TABLE_RULES
        if policy_for(DataConsumer.DELETE, rule.table).policy
        in _DIRECTLY_PURGED_POLICIES
    )


class SACharacterDataEraser:
    """The SQL half of a character delete, derived from the registry."""

    def __init__(
        self, session_factory: "sessionmaker[AsyncSession]",
    ) -> None:
        self._session_factory = session_factory

    # -- step 1: harvest --------------------------------------------------

    async def collect_media_object_urls(
        self, character_id: str,
    ) -> tuple[str, ...]:
        """Every media URL this character's rows still point at.

        Must run **before** :meth:`erase`: these URLs live in the rows,
        and ``messages.attachments_json`` in particular points into the
        operator's own ``users/{user_id}/chat-uploads/`` subtree, which no
        character-scoped prefix purge can reach.
        """
        urls: list[str] = []
        seen: set[str] = set()
        async with self._session_factory() as session:
            for table_name, fields in media_url_fields_by_table().items():
                rule = BACKUP_TABLE_RULES_BY_NAME[table_name]
                table = Base.metadata.tables[table_name]
                stmt = select(
                    *(table.c[spec.column] for spec in fields),
                ).where(character_predicate(table, rule, character_id))
                if rule.row_filter:
                    stmt = stmt.where(text(rule.row_filter))
                result = await session.stream(
                    stmt.execution_options(yield_per=_STREAM_BATCH_ROWS),
                )
                async for row in result:
                    for spec, value in zip(fields, row):
                        for url in media_urls_from_value(spec, value):
                            if url in seen:
                                continue
                            seen.add(url)
                            urls.append(url)
        return tuple(urls)

    async def owner_user_id(self, character_id: str) -> str | None:
        """The operator this character belongs to (CD2 containment).

        The harvest is allowed to delete objects under the *operator's*
        chat-upload subtree, which is not derivable from the character id
        alone. Answering ``None`` (unknown character) is what makes the
        service skip that subtree entirely rather than widen the sweep.
        """
        table = Base.metadata.tables["characters"]
        async with self._session_factory() as session:
            result = await session.execute(
                select(table.c.user_id).where(
                    table.c.id == character_id,
                ).limit(1),
            )
            return result.scalar_one_or_none()

    # -- step 2: purge ----------------------------------------------------

    async def erase(self, character_id: str) -> DatabaseErasureResult:
        """One transaction, reverse registry order, character row last."""
        rows_deleted: dict[str, int] = {}
        character_existed = False
        async with self._session_factory() as session:
            for rule in reversed(CHARACTER_BACKUP_TABLE_RULES):
                policy = policy_for(DataConsumer.DELETE, rule.table).policy
                if policy not in _EMITTING_POLICIES:
                    continue
                table = Base.metadata.tables[rule.table]
                stmt = delete(table).where(
                    _delete_predicate(table, rule, policy, character_id),
                )
                if rule.row_filter:
                    stmt = stmt.where(text(rule.row_filter))
                result = await session.execute(stmt)
                count = int(result.rowcount or 0)
                if rule.table == "characters":
                    character_existed = count > 0
                if count:
                    rows_deleted[rule.table] = count
            await session.commit()
        return DatabaseErasureResult(
            character_existed=character_existed,
            rows_deleted=MappingProxyType(rows_deleted),
        )


def _delete_predicate(
    table: Table, rule, policy: DeletePolicy, character_id: str,
) -> ColumnElement[bool]:
    if policy is DeletePolicy.PURGE_ANY_SIDE:
        return any_side_character_predicate(table, character_id)
    if policy is DeletePolicy.PURGE_VIA_PARENT:
        return via_parent_predicate(table, rule, character_id)
    return character_predicate(table, rule, character_id)


def via_parent_predicate(
    table: Table, rule, character_id: str,
) -> ColumnElement[bool]:
    """The rows a ``PURGE_VIA_PARENT`` table owes to this character.

    Scoped through the parent the ``ON DELETE CASCADE`` names, narrowed
    by ``rule.parent_table`` where the registry declares one. See
    :mod:`sa_character_parent_links` for why the FK verb and the parent's
    own fate both have to be checked.
    """
    return parent_link_predicate(
        table,
        character_id,
        ondelete=CASCADE,
        removed_tables=directly_purged_tables(),
        only_parent=rule.parent_table,
    )


def any_side_character_columns(table: Table) -> tuple[Column, ...]:
    """The two (or more) character columns of a cross-character table.

    Raises when fewer than two are found: ``PURGE_ANY_SIDE`` on a
    single-sided table means the registry and the schema disagree, and a
    delete that silently degrades to one side is exactly the half-deleted
    relationship this policy exists to prevent.
    """
    columns = tuple(
        column
        for column in table.columns
        if _CHARACTER_REF_COLUMN.search(column.name)
    )
    if len(columns) < 2:
        raise LookupError(
            f"table {table.name} is classified PURGE_ANY_SIDE but carries "
            f"{len(columns)} character-reference column(s) — registry and "
            "schema disagree",
        )
    return columns


def any_side_character_predicate(
    table: Table, character_id: str,
) -> ColumnElement[bool]:
    """``OR`` over every side — either end matching removes the row."""
    return or_(
        *(
            column == character_id
            for column in any_side_character_columns(table)
        ),
    )
