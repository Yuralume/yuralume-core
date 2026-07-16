"""character encounters and schedule participants

Revision ID: cd9i1j60054
Revises: cc8h0i50053
Create Date: 2026-05-17 22:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cd9i1j60054"
down_revision: Union[str, None] = "cc8h0i50053"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "schedule_activities",
        sa.Column(
            "participant_refs_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )

    op.add_column(
        "character_relationships",
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "character_relationships",
        sa.Column("how_a_sees_b", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "character_relationships",
        sa.Column("how_b_sees_a", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "character_relationships",
        sa.Column(
            "affection_a_to_b",
            sa.Integer(),
            nullable=False,
            server_default="50",
        ),
    )
    op.add_column(
        "character_relationships",
        sa.Column(
            "affection_b_to_a",
            sa.Integer(),
            nullable=False,
            server_default="50",
        ),
    )
    op.add_column(
        "character_relationships",
        sa.Column(
            "trust_a_to_b",
            sa.Integer(),
            nullable=False,
            server_default="50",
        ),
    )
    op.add_column(
        "character_relationships",
        sa.Column(
            "trust_b_to_a",
            sa.Integer(),
            nullable=False,
            server_default="50",
        ),
    )
    op.add_column(
        "character_relationships",
        sa.Column("last_interaction_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_character_relationships_canonical_pair",
        "character_relationships",
        ["from_character_id", "to_character_id"],
    )

    op.create_table(
        "character_encounters",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("relationship_id", sa.String(length=36), nullable=False),
        sa.Column("character_a_id", sa.String(length=36), nullable=False),
        sa.Column("character_b_id", sa.String(length=36), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("location", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="planned",
        ),
        sa.Column("trigger_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("max_turns", sa.Integer(), nullable=False, server_default="4"),
        sa.Column("transcript_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("summary_for_a", sa.Text(), nullable=False, server_default=""),
        sa.Column("summary_for_b", sa.Text(), nullable=False, server_default=""),
        sa.Column("memory_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_character_encounters_relationship",
        "character_encounters",
        ["relationship_id", "scheduled_for"],
    )
    op.create_index(
        "ix_character_encounters_a",
        "character_encounters",
        ["character_a_id", "scheduled_for"],
    )
    op.create_index(
        "ix_character_encounters_b",
        "character_encounters",
        ["character_b_id", "scheduled_for"],
    )
    op.create_index(
        "ix_character_encounters_status_due",
        "character_encounters",
        ["status", "scheduled_for"],
    )


def downgrade() -> None:
    op.drop_index("ix_character_encounters_status_due", table_name="character_encounters")
    op.drop_index("ix_character_encounters_b", table_name="character_encounters")
    op.drop_index("ix_character_encounters_a", table_name="character_encounters")
    op.drop_index("ix_character_encounters_relationship", table_name="character_encounters")
    op.drop_table("character_encounters")
    op.drop_constraint(
        "uq_character_relationships_canonical_pair",
        "character_relationships",
        type_="unique",
    )
    op.drop_column("character_relationships", "last_interaction_at")
    op.drop_column("character_relationships", "trust_b_to_a")
    op.drop_column("character_relationships", "trust_a_to_b")
    op.drop_column("character_relationships", "affection_b_to_a")
    op.drop_column("character_relationships", "affection_a_to_b")
    op.drop_column("character_relationships", "how_b_sees_a")
    op.drop_column("character_relationships", "how_a_sees_b")
    op.drop_column("character_relationships", "enabled")
    op.drop_column("schedule_activities", "participant_refs_json")
