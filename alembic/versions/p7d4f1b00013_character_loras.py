"""add characters.loras_json column

Stores the LoRA weights (name + strength) applied when generating
images for the character via ComfyUI. JSON-encoded on the row to
mirror ``image_urls`` and ``allowed_tools``; tiny per-character list
not worth a join table.

Revision ID: p7d4f1b00013
Revises: o6c3e0a90012
Create Date: 2026-04-19 22:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "p7d4f1b00013"
down_revision: Union[str, None] = "o6c3e0a90012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "loras_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "loras_json")
