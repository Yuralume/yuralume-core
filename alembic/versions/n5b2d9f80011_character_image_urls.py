"""add character.image_urls column

Portraits are now plain images uploaded to ``uploads/characters/<id>/``.
We store their URLs as a JSON-encoded list on the character row — the
list is tiny (typical: 1-5 entries) so a dedicated join table would be
overkill.

Revision ID: n5b2d9f80011
Revises: m4a1c8e70010
Create Date: 2026-04-19 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "n5b2d9f80011"
down_revision: Union[str, None] = "m4a1c8e70010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "image_urls",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "image_urls")
