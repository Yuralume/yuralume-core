"""add memory_items.audience

Feed shareability classified by the post-turn extractor: ``private``
(relationship book-keeping / naming preferences / secrets the character
would never broadcast), ``shareable`` (ordinary life moments), or ``""``
(legacy / unjudged). The LumeGram feed collector skips ``private`` rows so
a private preference never becomes a public post; recall in chat is
unaffected. Existing rows backfill to ``""`` (feed-eligible) via the
server_default so the back catalogue is never silenced.

Revision ID: r4c6f8t00203
Revises: q3b5e7s90202
Create Date: 2026-06-30 00:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "r4c6f8t00203"
down_revision: Union[str, None] = "q3b5e7s90202"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "memory_items",
        sa.Column(
            "audience",
            sa.String(length=16),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("memory_items", "audience")
