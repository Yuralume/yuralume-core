"""add initial relationship seed table

Revision ID: n7d4f1h00090
Revises: n6c3e0g90089
Create Date: 2026-06-11 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "n7d4f1h00090"
down_revision: Union[str, None] = "n6c3e0g90089"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "character_operator_relationship_seeds",
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "operator_id",
            sa.String(length=64),
            sa.ForeignKey("operator_profiles.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("relationship_label", sa.Text(), nullable=False, server_default=""),
        sa.Column("known_context", sa.Text(), nullable=False, server_default=""),
        sa.Column("user_address_name", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "character_address_name", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column("tone_distance", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "familiarity_boundary", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column(
            "schedule_involvement_policy",
            sa.String(length=32),
            nullable=False,
            server_default="none",
        ),
        sa.Column(
            "proactive_permission",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "proactive_cadence_hint", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column("user_profile_notes", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "confirmed_by_user",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "character_id",
            "operator_id",
            name="uq_character_operator_relationship_seed_pair",
        ),
    )


def downgrade() -> None:
    op.drop_table("character_operator_relationship_seeds")
