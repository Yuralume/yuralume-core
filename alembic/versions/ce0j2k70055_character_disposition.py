"""character disposition

Revision ID: ce0j2k70055
Revises: cd9i1j60054
Create Date: 2026-05-18 10:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "ce0j2k70055"
down_revision: Union[str, None] = "cd9i1j60054"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # "{}" is the canonical "all medium / 沒設定" sentinel — repository's
    # _disposition_from_json decodes it to CharacterDisposition.DEFAULT,
    # so 既有 row 不需要 backfill 任何值。
    op.add_column(
        "characters",
        sa.Column(
            "disposition_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "disposition_json")
