"""add characters.image_trigger_patterns

Regex patterns that force ``ChatService`` to call ``generate_image``
without routing through the LLM tool-selection hop. Empty list ([])
keeps the feature off for existing characters so the upgrade is a
no-op behavioural change.

Revision ID: v4d0f7c20019
Revises: u3c9e6b10018
Create Date: 2026-04-21 11:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "v4d0f7c20019"
down_revision: Union[str, None] = "u3c9e6b10018"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "image_trigger_patterns",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "image_trigger_patterns")
