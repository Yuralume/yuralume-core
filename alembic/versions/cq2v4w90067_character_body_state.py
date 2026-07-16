"""characters.body_state_json — embodied signal quad

Revision ID: cq2v4w90067
Revises: cp1u3v80066
Create Date: 2026-05-21 09:00:00.000000

HUMANIZATION_ROADMAP §4.1 — embodied signals: hunger / thirst /
sleep_debt / seasonal_allergy as a four-band qualitative VO. Owner
decision (2026-05-21): no menstrual phase in this wave.

Empty JSON / NULL = "all low" (default = no body discomfort). Mirrors
the ``disposition_json`` column added by ``ce0j2k70055``.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cq2v4w90067"
down_revision: Union[str, None] = "cp1u3v80066"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "characters",
        sa.Column(
            "body_state_json",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )


def downgrade() -> None:
    op.drop_column("characters", "body_state_json")
