"""operator location fields

Revision ID: da2f4g70077
Revises: e4k6l1m20081
Create Date: 2026-06-03
"""

from __future__ import annotations

from typing import Union

from alembic import op
import sqlalchemy as sa


revision: str = "da2f4g70077"
down_revision: Union[str, None] = "e4k6l1m20081"
branch_labels: Union[str, None] = None
depends_on: Union[str, None] = None


def upgrade() -> None:
    op.add_column(
        "operator_profiles",
        sa.Column("country_code", sa.String(length=2), nullable=True),
    )
    op.add_column(
        "operator_profiles",
        sa.Column("latitude", sa.Float(), nullable=True),
    )
    op.add_column(
        "operator_profiles",
        sa.Column("longitude", sa.Float(), nullable=True),
    )
    op.add_column(
        "operator_profiles",
        sa.Column("location_label", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("operator_profiles", "location_label")
    op.drop_column("operator_profiles", "longitude")
    op.drop_column("operator_profiles", "latitude")
    op.drop_column("operator_profiles", "country_code")
