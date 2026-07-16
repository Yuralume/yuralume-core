"""persona curiosity attempts

Revision ID: h8p0q6r70085
Revises: g7n9o5p60084
Create Date: 2026-06-09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "h8p0q6r70085"
down_revision = "g7n9o5p60084"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "persona_curiosity_attempts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=64), nullable=True),
        sa.Column("surface", sa.String(length=24), nullable=False),
        sa.Column("target_layer", sa.Integer(), nullable=False),
        sa.Column("target_topic", sa.String(length=80), nullable=False),
        sa.Column("question_intent", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("response_turn_id", sa.String(length=64), nullable=True),
        sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
        sa.ForeignKeyConstraint(
            ["character_id"],
            ["characters.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_persona_curiosity_attempts_character_id",
        "persona_curiosity_attempts",
        ["character_id"],
    )
    op.create_index(
        "ix_persona_curiosity_attempts_operator_id",
        "persona_curiosity_attempts",
        ["operator_id"],
    )
    op.create_index(
        "ix_persona_curiosity_attempts_status",
        "persona_curiosity_attempts",
        ["status"],
    )
    op.create_index(
        "ix_persona_curiosity_attempts_surface",
        "persona_curiosity_attempts",
        ["surface"],
    )
    op.create_index(
        "ix_persona_curiosity_attempts_created_at",
        "persona_curiosity_attempts",
        ["created_at"],
    )
    op.create_index(
        "ix_persona_curiosity_attempts_pair_created_at",
        "persona_curiosity_attempts",
        ["character_id", "operator_id", "created_at"],
    )
    op.alter_column("persona_curiosity_attempts", "metadata_json", server_default=None)


def downgrade() -> None:
    op.drop_index(
        "ix_persona_curiosity_attempts_pair_created_at",
        table_name="persona_curiosity_attempts",
    )
    op.drop_index(
        "ix_persona_curiosity_attempts_created_at",
        table_name="persona_curiosity_attempts",
    )
    op.drop_index(
        "ix_persona_curiosity_attempts_surface",
        table_name="persona_curiosity_attempts",
    )
    op.drop_index(
        "ix_persona_curiosity_attempts_status",
        table_name="persona_curiosity_attempts",
    )
    op.drop_index(
        "ix_persona_curiosity_attempts_operator_id",
        table_name="persona_curiosity_attempts",
    )
    op.drop_index(
        "ix_persona_curiosity_attempts_character_id",
        table_name="persona_curiosity_attempts",
    )
    op.drop_table("persona_curiosity_attempts")
