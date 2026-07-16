"""memoir_pins table — player-side memoir pin store

Revision ID: cw8b0c30073
Revises: cv7a9b20072
Create Date: 2026-05-25 09:00:00.000000

docs/MEMOIR_PLAN.md — pinning lets the player promote a memoir entry to
the top of their timeline. Unique on
``(character_id, operator_id, entry_kind, entry_id)`` so re-pinning is a
no-op and the per-(character, operator) isolation rule is enforced at
the schema level.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cw8b0c30073"
down_revision: Union[str, None] = "cv7a9b20072"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "memoir_pins",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column("entry_kind", sa.String(length=16), nullable=False),
        sa.Column("entry_id", sa.String(length=64), nullable=False),
        sa.Column("pinned_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "character_id", "operator_id", "entry_kind", "entry_id",
            name="uq_memoir_pins_per_pair_entry",
        ),
    )
    # Composite index for "list pins for pair" — same leftmost columns
    # as the unique constraint, so this is technically redundant on
    # most engines, but explicit naming helps `\d memoir_pins` reviewers.
    op.create_index(
        "ix_memoir_pins_pair",
        "memoir_pins",
        ["character_id", "operator_id"],
    )
    op.create_index(
        "ix_memoir_pins_pinned_at",
        "memoir_pins",
        ["pinned_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_memoir_pins_pinned_at", table_name="memoir_pins")
    op.drop_index("ix_memoir_pins_pair", table_name="memoir_pins")
    op.drop_table("memoir_pins")
