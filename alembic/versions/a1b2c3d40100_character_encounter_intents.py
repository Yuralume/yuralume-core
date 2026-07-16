"""Add character encounter intents.

Revision ID: a1b2c3d40100
Revises: t6w3x0y10099
Create Date: 2026-06-20 00:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "a1b2c3d40100"
down_revision = "t6w3x0y10099"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "character_encounter_intents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("peer_character_id", sa.String(length=36), nullable=False),
        sa.Column("desired_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("topic", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="chat_agreement",
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("source_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_character_encounter_intents_character_id",
        "character_encounter_intents",
        ["character_id"],
    )
    op.create_index(
        "ix_character_encounter_intents_peer_character_id",
        "character_encounter_intents",
        ["peer_character_id"],
    )
    op.create_index(
        "ix_character_encounter_intents_status",
        "character_encounter_intents",
        ["status"],
    )
    op.create_index(
        "ix_character_encounter_intents_desired_after",
        "character_encounter_intents",
        ["desired_after"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_character_encounter_intents_desired_after",
        table_name="character_encounter_intents",
    )
    op.drop_index(
        "ix_character_encounter_intents_status",
        table_name="character_encounter_intents",
    )
    op.drop_index(
        "ix_character_encounter_intents_peer_character_id",
        table_name="character_encounter_intents",
    )
    op.drop_index(
        "ix_character_encounter_intents_character_id",
        table_name="character_encounter_intents",
    )
    op.drop_table("character_encounter_intents")
