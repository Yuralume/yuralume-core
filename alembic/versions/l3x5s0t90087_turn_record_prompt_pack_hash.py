"""turn record prompt pack hash

Revision ID: l3x5s0t90087
Revises: m4n6p8q00088
Create Date: 2026-06-11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "l3x5s0t90087"
down_revision = "m4n6p8q00088"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "turn_records",
        sa.Column(
            "prompt_pack_hash",
            sa.String(length=64),
            nullable=False,
            server_default="",
        ),
    )
    op.alter_column("turn_records", "prompt_pack_hash", server_default=None)
    op.create_index(
        "ix_turn_records_prompt_pack_hash",
        "turn_records",
        ["prompt_pack_hash"],
    )


def downgrade() -> None:
    op.drop_index("ix_turn_records_prompt_pack_hash", table_name="turn_records")
    op.drop_column("turn_records", "prompt_pack_hash")
