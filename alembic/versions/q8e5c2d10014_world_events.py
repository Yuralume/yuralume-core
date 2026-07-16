"""add world_events table + pgvector HNSW index

External-world event pool (news, RSS feeds, social). Kept separate from
``memory_items`` on purpose: these are *reference material*, not the
character's subjective memory.

Revision ID: q8e5c2d10014
Revises: p7d4f1b00013
Create Date: 2026-04-19 23:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision: str = "q8e5c2d10014"
down_revision: Union[str, None] = "p7d4f1b00013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DIM = 1024
_INDEX_NAME = "ix_world_events_embedding_hnsw"


def upgrade() -> None:
    op.create_table(
        "world_events",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("source", sa.String(length=80), nullable=False, index=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.Text(), nullable=False, unique=True),
        sa.Column(
            "published_at",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "topic_tags",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("embedding", Vector(_DIM), nullable=True),
    )
    op.execute(
        f"CREATE INDEX {_INDEX_NAME} ON world_events "
        f"USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
    op.drop_table("world_events")
