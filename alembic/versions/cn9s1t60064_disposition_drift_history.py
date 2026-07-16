"""disposition_drift_history table — dream-time band shift audit

Revision ID: cn9s1t60064
Revises: cm8r0s50063
Create Date: 2026-05-21 05:30:00.000000

HUMANIZATION_ROADMAP §3.1 — the dream pass periodically asks the LLM
whether one ``CharacterDisposition`` dimension should nudge its band.
Each accepted shift writes a row here for the audit timeline and for
30-day-cooldown enforcement.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cn9s1t60064"
down_revision: Union[str, None] = "cm8r0s50063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "disposition_drift_history",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("dimension", sa.String(length=32), nullable=False),
        sa.Column("from_band", sa.String(length=8), nullable=False),
        sa.Column("to_band", sa.String(length=8), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence_quote", sa.Text(), nullable=False, server_default=""),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_disposition_drift_history_character_id",
        "disposition_drift_history",
        ["character_id"],
    )
    op.create_index(
        "ix_disposition_drift_history_dimension",
        "disposition_drift_history",
        ["dimension"],
    )
    op.create_index(
        "ix_disposition_drift_history_decided_at",
        "disposition_drift_history",
        ["decided_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_disposition_drift_history_decided_at",
        table_name="disposition_drift_history",
    )
    op.drop_index(
        "ix_disposition_drift_history_dimension",
        table_name="disposition_drift_history",
    )
    op.drop_index(
        "ix_disposition_drift_history_character_id",
        table_name="disposition_drift_history",
    )
    op.drop_table("disposition_drift_history")
