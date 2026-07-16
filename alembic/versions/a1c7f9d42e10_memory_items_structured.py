"""memory_items_structured

Replace the flat ``memory_snippets`` table with a structured
``memory_items`` table. Old snippet data is intentionally dropped —
the project is still pre-release and the previous schema stored
unstructured turn dumps that cannot be mapped onto the new shape.

Revision ID: a1c7f9d42e10
Revises: e78f4b21f30b
Create Date: 2026-04-15 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1c7f9d42e10'
down_revision: Union[str, Sequence[str], None] = 'e78f4b21f30b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index(op.f('ix_memory_snippets_character_id'), table_name='memory_snippets')
    op.drop_table('memory_snippets')

    op.create_table(
        'memory_items',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('character_id', sa.String(length=36), nullable=False),
        sa.Column('conversation_id', sa.String(length=36), nullable=True),
        sa.Column('kind', sa.String(length=32), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('salience', sa.Float(), nullable=False),
        sa.Column('tags', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_accessed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('access_count', sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_memory_items_character_id'), 'memory_items', ['character_id'], unique=False)
    op.create_index(op.f('ix_memory_items_kind'), 'memory_items', ['kind'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_memory_items_kind'), table_name='memory_items')
    op.drop_index(op.f('ix_memory_items_character_id'), table_name='memory_items')
    op.drop_table('memory_items')

    op.create_table(
        'memory_snippets',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('character_id', sa.String(length=36), nullable=False),
        sa.Column('conversation_id', sa.String(length=36), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_memory_snippets_character_id'), 'memory_snippets', ['character_id'], unique=False)
