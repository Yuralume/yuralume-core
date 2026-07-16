"""branching dramas

Three new tables for the branching-drama (分歧劇場) feature:

- ``branching_dramas``          — metadata + generation status
- ``branching_drama_nodes``     — tree nodes (segment variants)
- ``branching_drama_sessions``  — player playthrough state

Nodes form a tree via ``parent_node_id`` self-reference (no FK
constraint — the self-ref would complicate bulk inserts during
tree generation).

Revision ID: br7v9w40042
Revises: bq6u8v30041
Create Date: 2026-05-07 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "br7v9w40042"
down_revision: Union[str, None] = "bq6u8v30041"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "branching_dramas",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_ids_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "total_segments",
            sa.Integer(),
            nullable=False,
            server_default="6",
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="generating_outlines",
        ),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
        ),
    )
    op.create_index(
        "ix_branching_dramas_status",
        "branching_dramas",
        ["status"],
    )
    op.create_index(
        "ix_branching_dramas_updated_at",
        "branching_dramas",
        ["updated_at"],
    )

    op.create_table(
        "branching_drama_nodes",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "drama_id",
            sa.String(length=36),
            sa.ForeignKey("branching_dramas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "parent_node_id",
            sa.String(length=36),
            nullable=True,
        ),
        sa.Column(
            "depth",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("tone", sa.String(length=16), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column(
            "appearing_character_ids_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("image_path", sa.Text(), nullable=True),
    )
    op.create_index(
        "ix_branching_drama_nodes_drama_id",
        "branching_drama_nodes",
        ["drama_id"],
    )
    op.create_index(
        "ix_branching_drama_nodes_parent_node_id",
        "branching_drama_nodes",
        ["parent_node_id"],
    )

    op.create_table(
        "branching_drama_sessions",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "drama_id",
            sa.String(length=36),
            sa.ForeignKey("branching_dramas.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "current_node_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="playing",
        ),
        sa.Column(
            "turns_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
        ),
    )
    op.create_index(
        "ix_branching_drama_sessions_drama_id",
        "branching_drama_sessions",
        ["drama_id"],
    )
    op.create_index(
        "ix_branching_drama_sessions_updated_at",
        "branching_drama_sessions",
        ["updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_branching_drama_sessions_updated_at",
        table_name="branching_drama_sessions",
    )
    op.drop_index(
        "ix_branching_drama_sessions_drama_id",
        table_name="branching_drama_sessions",
    )
    op.drop_table("branching_drama_sessions")
    op.drop_index(
        "ix_branching_drama_nodes_parent_node_id",
        table_name="branching_drama_nodes",
    )
    op.drop_index(
        "ix_branching_drama_nodes_drama_id",
        table_name="branching_drama_nodes",
    )
    op.drop_table("branching_drama_nodes")
    op.drop_index(
        "ix_branching_dramas_updated_at",
        table_name="branching_dramas",
    )
    op.drop_index(
        "ix_branching_dramas_status",
        table_name="branching_dramas",
    )
    op.drop_table("branching_dramas")
