"""Parent-link predicates for the character-boundary erasers (CD2/CD3).

``PURGE_VIA_PARENT`` and ``CLEAR_REFERENCE`` describe rows that leave (or
lose a pointer) through a foreign key's ``ON DELETE`` action. The erasers
used to read those two verbs as "emit nothing, the database will do it" —
**which is only true where foreign keys are enforced**. PostgreSQL always
enforces them; SQLite enforces them only under ``PRAGMA foreign_keys=ON``,
and :mod:`kokoro_link.infrastructure.persistence.engine` never sets it. So
on every self-host SQLite deployment the cascade silently did nothing and
``messages`` / ``schedule_activities`` / ``story_arc_beats`` /
``feed_reactions`` / ``feed_comments`` / ``channel_bindings`` survived a
character delete.

This module makes both verbs **explicit statements** derived from the
schema, so the zero-orphan guarantee stops depending on a database-level
setting the application does not control. Where the CB0 registry already
names the parent (``BackupTableRule.parent_table``), that declaration
narrows the search — the registry stays the source of truth and this
module only fills in what it is silent about (``channel_bindings`` is a
B-layer table, so it never got a ``parent_table``).

Two rules keep the derivation honest rather than "any FK will do":

* **The ``ON DELETE`` verb must match the policy.** ``PURGE_VIA_PARENT``
  only follows ``CASCADE`` links, ``CLEAR_REFERENCE`` only ``SET NULL``
  ones. ``channel_bindings`` is why: it has *both* — ``account_id``
  cascades from ``messaging_accounts`` (delete), ``conversation_id``
  nulls from ``conversations`` (reset). Following the wrong one would
  delete a live binding during a chat-log reset.
* **The parent must be a table this operation actually removes.**
  ``story_scene_sessions`` cascades from both ``characters`` and
  ``conversations``; during ``reset(conversations=True)`` only the latter
  is being purged, so only the latter may scope the delete.

The emitted predicate is an ``IN (SELECT parent.id WHERE …)`` subquery,
which means **order matters**: these statements must run while the parent
rows still exist, i.e. children before parents. Both erasers walk the
registry in reverse for exactly that reason.
"""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.schema import Table

CASCADE = "CASCADE"
SET_NULL = "SET NULL"


def parent_scope_predicate(
    parent: Table, character_id: str,
) -> ColumnElement[bool] | None:
    """How ``parent``'s rows are scoped to one character, or ``None``.

    ``None`` means "this parent cannot be scoped to a character", which
    is a legitimate answer (a FK to a shared/global table) and simply
    disqualifies that link rather than raising.
    """
    if parent.name == "characters":
        return parent.c.id == character_id
    if "character_id" in parent.c:
        return parent.c.character_id == character_id
    return None


def parent_links(
    table: Table,
    *,
    ondelete: str,
    removed_tables: frozenset[str],
    only_parent: str | None = None,
) -> tuple[tuple[ColumnElement, ColumnElement, Table], ...]:
    """``(child column, parent key column, parent table)`` per usable link.

    Filtered by the ``ON DELETE`` verb, by whether the parent is among
    the tables this operation removes, and — when the registry declared a
    ``parent_table`` — by that declaration.
    """
    wanted = " ".join(ondelete.upper().split())
    links = []
    for column in table.columns:
        for foreign_key in column.foreign_keys:
            action = " ".join((foreign_key.ondelete or "").upper().split())
            if action != wanted:
                continue
            parent = foreign_key.column.table
            if only_parent is not None and parent.name != only_parent:
                continue
            if parent.name not in removed_tables:
                continue
            links.append((column, foreign_key.column, parent))
    return tuple(links)


def parent_link_predicate(
    table: Table,
    character_id: str,
    *,
    ondelete: str,
    removed_tables: frozenset[str],
    only_parent: str | None = None,
) -> ColumnElement[bool]:
    """``child_fk IN (SELECT parent.id WHERE <character scope>)``.

    Raises :class:`LookupError` when no link qualifies: a table
    classified ``PURGE_VIA_PARENT`` / ``CLEAR_REFERENCE`` with no usable
    parent link means the policy and the schema disagree, and silently
    emitting nothing there is precisely the failure mode this module
    exists to remove.
    """
    clauses = []
    for column, parent_key, parent in parent_links(
        table,
        ondelete=ondelete,
        removed_tables=removed_tables,
        only_parent=only_parent,
    ):
        scope = parent_scope_predicate(parent, character_id)
        if scope is None:
            continue
        clauses.append(column.in_(select(parent_key).where(scope)))
    if not clauses:
        raise LookupError(
            f"table {table.name} has no ON DELETE {ondelete} foreign key to "
            "a character-scoped table being removed — the consumer policy "
            "and the schema disagree",
        )
    return or_(*clauses)
