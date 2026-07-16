"""add messages.kind

Tags each message as ``chat`` (normal dialogue) or ``tool_only`` (bare
tool-call artifact such as a ``/pic`` image with empty text). Downstream
consumers (schedule / arc / proactive generators) filter out
``tool_only`` when condensing dialogue context so a wall of image-only
turns doesn't dominate the summary.

Existing rows default to ``chat`` — this keeps prior behaviour intact.

Revision ID: x6f2b9e40021
Revises: w5e1a8d30020
Create Date: 2026-04-22 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "x6f2b9e40021"
down_revision: Union[str, None] = "w5e1a8d30020"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "kind",
            sa.String(length=16),
            nullable=False,
            server_default="chat",
        ),
    )


def downgrade() -> None:
    op.drop_column("messages", "kind")
