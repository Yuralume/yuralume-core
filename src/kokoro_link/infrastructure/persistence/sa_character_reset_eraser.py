"""Registry-driven SQL erasure of one character's RESET boundary (CD3).

Twin of :mod:`sa_character_data_eraser`, narrowed from "everything the
DELETE policy purges" to "whatever the requested reset flags declare" —
same registry, same ``character_predicate`` reuse, different consumer
axis (:data:`~kokoro_link.application.dto.character_backup.DataConsumer.RESET`
instead of ``DELETE``, resolved here through
``consumer_policies.reset_effects_for_flag`` rather than ``policy_for``
per table, since a flag's table set is already the unit CD0 declares it
in).

What this module knows that the declaration cannot express:

* **Every declared effect gets an explicit statement — including the two
  that describe a database action.** ``PURGE_VIA_PARENT`` (FK ``ON
  DELETE CASCADE``) and ``CLEAR_REFERENCE`` (FK ``ON DELETE SET NULL``)
  used to emit nothing, on the reasoning that the database performs them
  the moment the ``PURGE`` table they hang off is cleared. That is only
  true where foreign keys are enforced — PostgreSQL always, SQLite only
  under ``PRAGMA foreign_keys=ON``, which
  :mod:`kokoro_link.infrastructure.persistence.engine` never sets — so on
  self-host SQLite ``reset(conversations=True)`` left every message, turn
  journal, unfulfilled promise and scene session behind, still pointing
  at a conversation that no longer existed. The statements are now
  derived from the FK the policy names (see
  :mod:`sa_character_parent_links`); ``CLEAR_REFERENCE`` emits an
  ``UPDATE … SET NULL``, never a ``DELETE`` — the binding row must
  survive, only its pointer drops out from under it.
* **Deletion order follows the CB0 registry, reversed** (children
  before parents), not flag-declaration order — the same ordering
  ``SACharacterDataEraser`` uses. That ordering is now load-bearing
  rather than merely tidy: every derived statement scopes itself with an
  ``IN (SELECT … FROM parent)`` subquery, so it has to run while the
  parent rows still exist. The registry declares parents before children
  and ``channel_bindings`` last of all, which puts the ``SET NULL``
  ahead of the ``conversations`` purge for free.
* **The *reported* count stays the primary table's only** — unchanged
  from pre-CD3, and unaffected by whether the children left via an
  explicit statement or a cascade.
"""

from __future__ import annotations

from sqlalchemy import delete, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import sessionmaker

from kokoro_link.application.dto.character_backup import (
    CHARACTER_BACKUP_TABLE_RULES,
)
from kokoro_link.application.dto.character_backup.consumer_policies import (
    ResetFlag,
    ResetPolicy,
    reset_effects_for_flag,
    reset_primary_table_for_flag,
)
from kokoro_link.application.services.character_reset import (
    CharacterResetCounts,
)
from kokoro_link.infrastructure.persistence.models import Base
from kokoro_link.infrastructure.persistence.sa_character_backup_export_reader import (
    character_predicate,
)
from kokoro_link.infrastructure.persistence.sa_character_parent_links import (
    CASCADE,
    SET_NULL,
    parent_link_predicate,
    parent_links,
)


class SACharacterResetEraser:
    """The SQL half of ``reset_character_data``, derived from the RESET
    consumer policy."""

    def __init__(
        self, session_factory: "sessionmaker[AsyncSession]",
    ) -> None:
        self._session_factory = session_factory

    async def erase(
        self, character_id: str, flags: frozenset[ResetFlag],
    ) -> CharacterResetCounts:
        """One transaction, registry order reversed, one statement per
        effect the requested flags declare.

        Reverse registry order is what makes the parent-scoped subqueries
        legal: children (and ``channel_bindings``' ``SET NULL``) run while
        their parent rows still exist.
        """
        if not flags:
            return {}
        effects = _requested_effects(flags)
        purged_tables = frozenset(
            table
            for table, policy in effects.items()
            if policy is ResetPolicy.PURGE
        )
        rowcounts: dict[str, int] = {}
        async with self._session_factory() as session:
            for rule in reversed(CHARACTER_BACKUP_TABLE_RULES):
                policy = effects.get(rule.table)
                if policy is None or policy is ResetPolicy.SKIP:
                    continue
                table = Base.metadata.tables[rule.table]
                stmt = _reset_statement(
                    table, rule, policy, character_id, purged_tables,
                )
                if rule.row_filter:
                    stmt = stmt.where(text(rule.row_filter))
                result = await session.execute(stmt)
                rowcounts[rule.table] = int(result.rowcount or 0)
            await session.commit()
        return {
            flag: rowcounts.get(reset_primary_table_for_flag(flag), 0)
            for flag in flags
        }


def _requested_effects(
    flags: frozenset[ResetFlag],
) -> dict[str, ResetPolicy]:
    """Table → policy for everything the requested flags declare.

    A table named by two active flags keeps the stronger verb: a
    ``PURGE`` from one flag must not be downgraded to the other's
    ``CLEAR_REFERENCE`` just because of declaration order.
    """
    effects: dict[str, ResetPolicy] = {}
    for flag in flags:
        for effect in reset_effects_for_flag(flag):
            current = effects.get(effect.table)
            if current is ResetPolicy.PURGE:
                continue
            if current is ResetPolicy.PURGE_VIA_PARENT and (
                effect.policy is ResetPolicy.CLEAR_REFERENCE
            ):
                continue
            effects[effect.table] = effect.policy
    return effects


def _reset_statement(
    table,
    rule,
    policy: ResetPolicy,
    character_id: str,
    purged_tables: frozenset[str],
):
    """One statement per declared effect, per its verb."""
    if policy is ResetPolicy.PURGE:
        return delete(table).where(
            character_predicate(table, rule, character_id),
        )
    if policy is ResetPolicy.PURGE_VIA_PARENT:
        return delete(table).where(
            parent_link_predicate(
                table,
                character_id,
                ondelete=CASCADE,
                removed_tables=purged_tables,
            ),
        )
    if policy is ResetPolicy.CLEAR_REFERENCE:
        return _clear_reference_statement(table, character_id, purged_tables)
    raise LookupError(
        f"reset has no statement shape for policy {policy!r} on "
        f"{table.name}",
    )


def _clear_reference_statement(table, character_id, purged_tables):
    """``UPDATE … SET <fk> = NULL`` for every pointer the reset severs.

    The columns are the ``ON DELETE SET NULL`` links into the tables this
    reset purges — the same derivation the cascade uses, read through the
    other FK verb, so the row survives with exactly the pointers the
    database would have nulled.
    """
    links = parent_links(
        table,
        ondelete=SET_NULL,
        removed_tables=purged_tables,
    )
    predicate = parent_link_predicate(
        table,
        character_id,
        ondelete=SET_NULL,
        removed_tables=purged_tables,
    )
    return update(table).where(predicate).values(
        {column.name: None for column, _key, _parent in links},
    )
