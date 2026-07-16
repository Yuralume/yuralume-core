"""arc template applicability

Revision ID: m4n6p8q00088
Revises: l3w5s9t00087
Create Date: 2026-06-10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "m4n6p8q00088"
down_revision = "l3w5s9t00087"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "arc_templates",
        sa.Column(
            "applicability_scope",
            sa.String(length=32),
            nullable=False,
            server_default="generic",
        ),
    )
    op.add_column(
        "arc_templates",
        sa.Column(
            "target_character_ids_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("arc_templates", "target_character_ids_json")
    op.drop_column("arc_templates", "applicability_scope")
