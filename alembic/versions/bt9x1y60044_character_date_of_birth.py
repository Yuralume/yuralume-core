"""character date_of_birth

Adds an optional ``date_of_birth`` column to ``characters``. The
field powers age / zodiac / "is today the character's birthday"
derivations in the prompt builder and the feed candidate collector.
All derivations are computed on read (no cached age column) so the
character "grows up" as real wall-clock time passes without any
backfill / migration churn each year.

NULL = unknown — backwards compatible with existing rows; nothing
downstream surfaces birthday hints when the column is NULL.

Revision ID: bt9x1y60044
Revises: bs8w0x50043
Create Date: 2026-05-11 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bt9x1y60044"
down_revision: Union[str, None] = "bs8w0x50043"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column("date_of_birth", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("characters", "date_of_birth")
