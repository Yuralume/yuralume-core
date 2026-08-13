"""character_backup_jobs — per-operator in-flight-restore dedup (CB A2).

Adversarial-review fix A2: two concurrent restores for the same operator
race the create gates (slot cap / daily limit only see committed rows —
a multi-minute landing window on a large archive is plenty of overlap)
and double-consume the same staged upload into duplicate characters and
duplicate ledger events. The schema-level backstop is the same portable
partial unique index shape as ``uq_character_backup_jobs_active_export``
(both supported dialects honour ``WHERE``-qualified unique indexes);
the repository ``add`` additionally re-checks inside its transaction and
raises the typed :class:`BackupJobConflictError`, so the invariant never
*depends* on the index existing.

Additive, no data change — safe to roll back.

Revision ID: g9x6r3u10035
Revises: f8w5q2t10034
Create Date: 2026-08-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "g9x6r3u10035"
down_revision = "f8w5q2t10034"
branch_labels = None
depends_on = None


_TABLE = "character_backup_jobs"
_INDEX = "uq_character_backup_jobs_active_restore"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        _TABLE,
        ["operator_id"],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'restore' AND status = 'running'",
        ),
        sqlite_where=sa.text(
            "kind = 'restore' AND status = 'running'",
        ),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name=_TABLE)
