"""add memory_items.embedding + pgvector HNSW index

Phase B — semantic memory retrieval. Adds a nullable ``vector(1024)``
column for BGE-M3 embeddings plus an HNSW index tuned for cosine
distance. The extension is created defensively here so a clean-slate
environment (fresh Postgres without the docker-compose init script,
e.g. a CI testcontainer) still works with just ``alembic upgrade head``.

Revision ID: h9b6e3a20005
Revises: g8a9d2f10004
Create Date: 2026-04-18 11:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "h9b6e3a20005"
down_revision: Union[str, None] = "g8a9d2f10004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DIM = 1024
_INDEX_NAME = "ix_memory_items_embedding_hnsw"


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.add_column(
        "memory_items",
        sa.Column("embedding", Vector(_DIM), nullable=True),
    )
    # HNSW with cosine_ops — keeps ``ORDER BY embedding <=> $1`` fast.
    # m/ef_construction left at pgvector defaults (16 / 64); fine for
    # the <10k rows we expect per character.
    op.execute(
        f"CREATE INDEX {_INDEX_NAME} ON memory_items "
        f"USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX_NAME}")
    op.drop_column("memory_items", "embedding")
