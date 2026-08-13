"""character_backup_jobs — durable ``.lumebackup`` export/restore jobs (CB0).

The one migration of the CB series (CHARACTER_FULL_BACKUP_PLAN §11:
"零角色資料 migration" — character data itself travels as DTO jsonl, so
only the job ledger needs schema).

Design notes carried in the ORM docstring
(``infrastructure/persistence/character_backup_models.py``):

- ``character_id`` is intentionally NOT a foreign key (background_jobs
  convention: a runtime record must degrade, never block a delete) and
  is nullable because a restore learns its character id only after
  creating it (D4: restore always creates a brand-new character).
- ``uq_character_backup_jobs_active_export`` — partial unique index over
  ``character_id`` restricted to in-flight exports — makes "at most one
  running export per character" a schema invariant. Both supported
  dialects honour partial indexes (same shape as
  ``uq_story_arcs_active_character``); the repository ``add`` re-checks
  transactionally anyway so the invariant never depends on the index.
- ``payload_json`` holds the password-wrapped file key only while the
  job runs; the repository erases it on every terminal transition.

Additive, no data change — safe to roll back.

Revision ID: f8w5q2t10034
Revises: e7v4p8s10033
Create Date: 2026-08-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "f8w5q2t10034"
down_revision = "e7v4p8s10033"
branch_labels = None
depends_on = None


_TABLE = "character_backup_jobs"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("character_id", sa.String(length=36), nullable=True),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="running",
        ),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default="1",
        ),
        sa.Column(
            "progress_json", sa.Text(), nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "payload_json", sa.Text(), nullable=False,
            server_default="{}",
        ),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("artifact_object_key", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "finished_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.create_index(
        "ix_character_backup_jobs_character_id",
        _TABLE,
        ["character_id"],
    )
    op.create_index(
        "uq_character_backup_jobs_active_export",
        _TABLE,
        ["character_id"],
        unique=True,
        postgresql_where=sa.text(
            "kind = 'export' AND status = 'running'",
        ),
        sqlite_where=sa.text(
            "kind = 'export' AND status = 'running'",
        ),
    )
    op.create_index(
        "ix_character_backup_jobs_status_updated",
        _TABLE,
        ["status", "updated_at"],
    )
    op.create_index(
        "ix_character_backup_jobs_operator_created",
        _TABLE,
        ["operator_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_character_backup_jobs_operator_created", table_name=_TABLE,
    )
    op.drop_index(
        "ix_character_backup_jobs_status_updated", table_name=_TABLE,
    )
    op.drop_index(
        "uq_character_backup_jobs_active_export", table_name=_TABLE,
    )
    op.drop_index(
        "ix_character_backup_jobs_character_id", table_name=_TABLE,
    )
    op.drop_table(_TABLE)
