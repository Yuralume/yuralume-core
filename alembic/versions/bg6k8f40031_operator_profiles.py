"""create operator_profiles table

Phase 1 of the world-system roadmap: introduce a real "operator"
entity so prompts and the post-turn extractor can refer to the human
by name instead of the hardcoded "使用者" role label. Single-row
semantics today (id="default"), but the table is keyed by id so a
future multi-operator world doesn't need another migration.

Revision ID: bg6k8f40031
Revises: af5j7e30030
Create Date: 2026-04-30 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "bg6k8f40031"
down_revision: Union[str, None] = "af5j7e30030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operator_profiles",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column(
            "aliases_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("pronouns", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("operator_profiles")
