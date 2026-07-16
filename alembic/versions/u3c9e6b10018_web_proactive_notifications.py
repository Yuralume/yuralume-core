"""add web-channel proactive notification fields

Adds two columns to ``characters``:

* ``accepts_web_proactive`` — opt-in (default True). When a character
  has ``proactive_enabled`` but no TG/LINE binding marked
  ``accepts_proactive``, the dispatcher's web path fires instead;
  operators who only want TG/LINE push can flip this off.
* ``unread_proactive_count`` — non-negative counter driving the red
  dot on the sidebar avatar. Incremented on every successful web
  delivery and zeroed by the mark-read endpoint.

Revision ID: u3c9e6b10018
Revises: t2b8f5a00017
Create Date: 2026-04-20 22:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "u3c9e6b10018"
down_revision: Union[str, None] = "t2b8f5a00017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "accepts_web_proactive",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "characters",
        sa.Column(
            "unread_proactive_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "unread_proactive_count")
    op.drop_column("characters", "accepts_web_proactive")
