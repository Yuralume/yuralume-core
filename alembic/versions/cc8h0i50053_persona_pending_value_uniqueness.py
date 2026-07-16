"""operator persona pending uniqueness includes value

Revision ID: cc8h0i50053
Revises: cb7f9g40052
Create Date: 2026-05-17 18:30:00.000000
"""

from typing import Sequence, Union

from alembic import op


revision: str = "cc8h0i50053"
down_revision: Union[str, None] = "cb7f9g40052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_operator_profile_fields_per_character_state",
        "operator_profile_fields",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_operator_profile_fields_per_character_state",
        "operator_profile_fields",
        ["character_id", "operator_id", "layer", "field_key", "state", "value"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_operator_profile_fields_per_character_state",
        "operator_profile_fields",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_operator_profile_fields_per_character_state",
        "operator_profile_fields",
        ["character_id", "operator_id", "layer", "field_key", "state"],
    )
