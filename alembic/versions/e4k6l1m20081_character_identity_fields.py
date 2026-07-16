"""character identity gender and pronoun fields

Revision ID: e4k6l1m20081
Revises: e3j5k0l10080
Create Date: 2026-06-01
"""

from alembic import op
import sqlalchemy as sa


revision = "e4k6l1m20081"
down_revision = "e3j5k0l10080"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "gender_identity",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "characters",
        sa.Column(
            "third_person_pronoun",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "characters",
        sa.Column(
            "visual_gender_presentation",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "visual_gender_presentation")
    op.drop_column("characters", "third_person_pronoun")
    op.drop_column("characters", "gender_identity")
