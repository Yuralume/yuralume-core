"""add characters.feature_models_json

Per-character LLM routing overrides — operator can pin
``{character A → Anthropic Sonnet, character B → LM Studio}`` without
touching the global ``feature_models`` preference. Empty list (``[]``)
means "no overrides; fall through to global preferences".

Revision ID: aa01b5g70025
Revises: ac2g4b90027
Create Date: 2026-04-27 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "aa01b5g70025"
down_revision: Union[str, None] = "ac2g4b90027"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "feature_models_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "feature_models_json")
