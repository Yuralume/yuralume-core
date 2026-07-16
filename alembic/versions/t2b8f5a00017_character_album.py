"""add character_album_items

Long-tail image archive per character, auto-populated by
``ComfyImageTool`` and manually populated by "move from stage" on the
settings panel.

Revision ID: t2b8f5a00017
Revises: s1a7e4f00016
Create Date: 2026-04-20 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "t2b8f5a00017"
down_revision: Union[str, None] = "s1a7e4f00016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "character_album_items",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column(
            "source",
            sa.String(length=16),
            nullable=False,
            server_default="tool",
        ),
        sa.Column("caption", sa.Text(), nullable=True),
        sa.Column("byte_size", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("character_album_items")
