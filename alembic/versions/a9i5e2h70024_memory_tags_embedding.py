"""add memory_items.tags_embedding column

Auxiliary embedding of the joined tag string per memory row, so
``query_semantic`` can boost retrieval when the user's query matches
the memory's *topic* but not its literal content phrasing. Same
1024-dim BGE-M3 vector as ``embedding``; HNSW index follows the same
shape (cosine ops, 16 connections, 64 build-time queue).

Revision ID: a9i5e2h70024
Revises: z8h4d1g60023
Create Date: 2026-04-25 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision: str = "a9i5e2h70024"
down_revision: Union[str, None] = "z8h4d1g60023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DIM = 1024


def upgrade() -> None:
    op.add_column(
        "memory_items",
        sa.Column("tags_embedding", Vector(_DIM), nullable=True),
    )
    # HNSW index — same shape as the existing content-embedding index
    # so query latency stays in the same ballpark when we add the
    # tag-cosine pass to ``query_semantic``.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memory_items_tags_embedding_hnsw "
        "ON memory_items USING hnsw (tags_embedding vector_cosine_ops) "
        "WITH (m = 16, ef_construction = 64);"
    )


def downgrade() -> None:
    op.execute(
        "DROP INDEX IF EXISTS ix_memory_items_tags_embedding_hnsw;"
    )
    op.drop_column("memory_items", "tags_embedding")
