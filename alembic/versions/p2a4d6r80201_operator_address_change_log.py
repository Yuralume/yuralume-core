"""add operator address change log (rename log)

Per-(character, operator, direction) audited address changes. Lets the
prompt builder surface the most recent rename as a relationship event
without rewriting historical memory. A global profile rename is bridged
through OperatorProfile.aliases, not this table.

Revision ID: p2a4d6r80201
Revises: m1e2r3g40200
Create Date: 2026-06-29 00:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "p2a4d6r80201"
down_revision: Union[str, None] = "m1e2r3g40200"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operator_address_change_log",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "operator_id",
            sa.String(length=64),
            sa.ForeignKey("operator_profiles.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("old_value", sa.Text(), nullable=False, server_default=""),
        sa.Column("new_value", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="player_edit",
        ),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_address_change_log_pair_direction",
        "operator_address_change_log",
        ["operator_id", "character_id", "direction", "effective_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_address_change_log_pair_direction",
        table_name="operator_address_change_log",
    )
    op.drop_table("operator_address_change_log")
