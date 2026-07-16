"""Add directional character peer profiles."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "t6w3x0y10099"
down_revision = "s5v2w9x10098"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "character_peer_profiles",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("peer_character_id", sa.String(length=36), nullable=False),
        sa.Column("peer_name", sa.String(length=160), nullable=False, server_default=""),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("occupation", sa.String(length=240), nullable=False, server_default=""),
        sa.Column("haunts_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("habits_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("relationship_note", sa.Text(), nullable=False, server_default=""),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0"),
        sa.Column("last_consolidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_memory_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "character_id",
            "peer_character_id",
            name="uq_character_peer_profiles_pair",
        ),
    )
    op.create_index(
        "ix_character_peer_profiles_character_id",
        "character_peer_profiles",
        ["character_id"],
    )
    op.create_index(
        "ix_character_peer_profiles_peer_character_id",
        "character_peer_profiles",
        ["peer_character_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_character_peer_profiles_peer_character_id",
        table_name="character_peer_profiles",
    )
    op.drop_index(
        "ix_character_peer_profiles_character_id",
        table_name="character_peer_profiles",
    )
    op.drop_table("character_peer_profiles")
