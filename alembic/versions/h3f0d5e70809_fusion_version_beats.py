"""fusion_story_versions.beats_json — per-beat prose snapshot (C0-6 restore)

Revision ID: h3f0d5e70809
Revises: g2e9c4d60708
Create Date: 2026-07-15
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "h3f0d5e70809"
down_revision = "g2e9c4d60708"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "fusion_story_versions",
        sa.Column(
            "beats_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("fusion_story_versions", "beats_json")
