"""operator_address_preferences — observed register / address style

Revision ID: cr3w5x90068
Revises: cq2v4w90067
Create Date: 2026-05-21 10:00:00.000000

HUMANIZATION_ROADMAP §4.2 — post-turn observation of how the operator
addresses each character (稱謂 / 敬語層級 / 回應長度偏好). One row per
``(character_id, operator_id)`` pair. Owner decision (2026-05-21): the
observed value overrides §3.6 ``operator_pace_preference`` when both
exist; the priority rule lives in the prompt builder, not in DB.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cr3w5x90068"
down_revision: Union[str, None] = "cq2v4w90067"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "operator_address_preferences",
        sa.Column("character_id", sa.String(length=36), primary_key=True),
        sa.Column("operator_id", sa.String(length=64), primary_key=True),
        sa.Column("salutation", sa.String(length=64), nullable=False, server_default=""),
        sa.Column(
            "formality_level", sa.String(length=16), nullable=False, server_default="medium",
        ),
        sa.Column(
            "response_length_pref",
            sa.String(length=16),
            nullable=False,
            server_default="medium",
        ),
        sa.Column("evidence_quote", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["character_id"], ["characters.id"], ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("operator_address_preferences")
