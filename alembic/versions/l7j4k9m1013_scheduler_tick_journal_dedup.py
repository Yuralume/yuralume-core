"""Make scheduler tick journal writes idempotent (SoakFix-A).

The journal has existed since P2-A without a logical uniqueness contract.  This
revision removes legacy duplicates before adding portable partial unique indexes:
character ticks are unique by ``(tick_bucket, kind, character_id)`` and the
cross-character social tick is unique by ``(tick_bucket, kind)``.

Revision ID: l7j4k9m1013
Revises: k6i3g8h01012
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "l7j4k9m1013"
down_revision = "k6i3g8h01012"
branch_labels = None
depends_on = None


_JOURNAL_DEDUP_SQL = sa.text(
    """
    DELETE FROM scheduler_tick_journal
    WHERE id IN (
        SELECT id
        FROM (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY tick_bucket, kind, character_id
                       ORDER BY id ASC
                   ) AS duplicate_rank
            FROM scheduler_tick_journal
        ) ranked
        WHERE duplicate_rank > 1
    )
    """
)


def upgrade() -> None:
    # Keep the lowest generated id for every logical entry.  The delete must
    # precede both indexes or a deployment containing legacy duplicates fails.
    op.execute(_JOURNAL_DEDUP_SQL)
    op.create_index(
        "uq_scheduler_tick_journal_character",
        "scheduler_tick_journal",
        ["tick_bucket", "kind", "character_id"],
        unique=True,
        postgresql_where=sa.text("character_id IS NOT NULL"),
        sqlite_where=sa.text("character_id IS NOT NULL"),
    )
    op.create_index(
        "uq_scheduler_tick_journal_social",
        "scheduler_tick_journal",
        ["tick_bucket", "kind"],
        unique=True,
        postgresql_where=sa.text("character_id IS NULL"),
        sqlite_where=sa.text("character_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_scheduler_tick_journal_social",
        table_name="scheduler_tick_journal",
    )
    op.drop_index(
        "uq_scheduler_tick_journal_character",
        table_name="scheduler_tick_journal",
    )
