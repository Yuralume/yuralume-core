"""Background execution-mode ownership row (P3-B, §2.2 / §13 Phase 3 / §15).

One ADDITIVE, single-row table that makes embedded / distributed background
execution mutually exclusive at the database level:

* ``background_execution_mode`` — ``name`` (pk, always ``'default'``),
  ``mode`` (``embedded``|``distributed``), ``epoch`` (monotonic CAS token),
  ``updated_at``.

No seed row: an ABSENT row is defined as ``('embedded', 0)`` (the self-host
red line), so a Self-host single container is unchanged and reads no ownership
until it explicitly flips into distributed mode. The first flip from epoch 0
INSERTs the row.

Revision ID: k6i3g8h01012
Revises: j5h2f7g90911
Create Date: 2026-07-21
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "k6i3g8h01012"
down_revision = "j5h2f7g90911"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "background_execution_mode",
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("mode", sa.String(length=16), nullable=False),
        sa.Column("epoch", sa.BigInteger(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("name"),
    )


def downgrade() -> None:
    op.drop_table("background_execution_mode")
