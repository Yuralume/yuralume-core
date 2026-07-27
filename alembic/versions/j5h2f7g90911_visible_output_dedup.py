"""Visible-output dedup constraints (P3-Dedup, §3.4/§6.2).

Two additive schema guards that close the visible per-character surfaces
that had NO DB-level dedup, so a reclaimed distributed job can no longer
race a still-running original worker into a double visible output:

* ``background_visible_slots`` — at-most-once claim ledger the proactive
  send + feed-comment-reply passes take before composing/delivering.
  ``UNIQUE(character_id, kind, slot)`` is the whole point: the losing
  executor's INSERT trips it and the pass skips silently.
* ``daily_schedules`` — the app-level upsert on ``(character_id, date)``
  was not DB-protected. We FIRST collapse any pre-existing duplicate rows
  (keep the newest per key), THEN add ``UNIQUE(character_id, date)``. The
  dedup DELETE must run BEFORE the constraint add or the ADD would fail on
  legacy dupes.

Encounter run-claim + schedule memorialize-claim are CAS UPDATEs on
existing columns (status / memorialized) — no schema change needed here.

Revision ID: j5h2f7g90911
Revises: i4g1e6f80810
Create Date: 2026-07-20
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "j5h2f7g90911"
down_revision = "i4g1e6f80810"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- daily_schedules: data-dedup FIRST, then the constraint ---------
    # Collapse duplicate (character_id, date) rows down to the newest one
    # (max generated_at; ties broken by max id) before adding the UNIQUE
    # constraint — otherwise the ADD would fail on any legacy dupe. Child
    # schedule_activities rows are removed by the FK ondelete=CASCADE.
    op.execute(
        sa.text(
            """
            DELETE FROM daily_schedules a
            USING daily_schedules b
            WHERE a.character_id = b.character_id
              AND a.date = b.date
              AND (
                a.generated_at < b.generated_at
                OR (a.generated_at = b.generated_at AND a.id < b.id)
              )
            """,
        ),
    )
    op.create_unique_constraint(
        "uq_daily_schedules_character_date",
        "daily_schedules",
        ["character_id", "date"],
    )

    # --- background_visible_slots: the claim ledger ---------------------
    op.create_table(
        "background_visible_slots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("slot", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "character_id", "kind", "slot",
            name="uq_background_visible_slots_claim",
        ),
    )
    op.create_index(
        "ix_background_visible_slots_created",
        "background_visible_slots",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_background_visible_slots_created",
        table_name="background_visible_slots",
    )
    op.drop_table("background_visible_slots")

    op.drop_constraint(
        "uq_daily_schedules_character_date",
        "daily_schedules",
        type_="unique",
    )
