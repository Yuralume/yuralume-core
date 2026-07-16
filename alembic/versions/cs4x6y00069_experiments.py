"""experiments + experiment_assignments — A/B framework

Revision ID: cs4x6y00069
Revises: cr3w5x90068
Create Date: 2026-05-21 11:00:00.000000

HUMANIZATION_ROADMAP §4.6 — sticky bucket framework. Owner decision
(2026-05-21): structured collection only, no auto winner detection.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cs4x6y00069"
down_revision: Union[str, None] = "cr3w5x90068"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "experiments",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("variants_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("salt", sa.String(length=32), nullable=False, server_default=""),
        sa.Column(
            "active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "experiment_assignments",
        sa.Column("experiment_id", sa.String(length=36), primary_key=True),
        sa.Column("character_id", sa.String(length=36), primary_key=True),
        sa.Column("operator_id", sa.String(length=64), primary_key=True),
        sa.Column("variant_id", sa.String(length=64), nullable=False),
        sa.Column(
            "assigned_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["experiment_id"], ["experiments.id"], ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["character_id"], ["characters.id"], ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("experiment_assignments")
    op.drop_table("experiments")
