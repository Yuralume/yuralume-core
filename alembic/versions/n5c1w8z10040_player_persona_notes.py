"""player_persona_notes — player-declared identity / world premise

Revision ID: n5c1w8z10040
Revises: k3b0v7y10039
Create Date: 2026-08-17 10:00:00.000000

PP series — the player writes their own setting for one relationship
(「我是超能力者」) and the character is asked to act on it as an
established fact. One row per ``(character_id, operator_id)``, permanent
until the player clears it (which deletes the row). Kept apart from the
inferred five-layer persona: a declaration has no confidence, no
evidence and no decay, so it must never enter a layer the dream pass can
merge or reject.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "n5c1w8z10040"
down_revision: Union[str, None] = "k3b0v7y10039"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "player_persona_notes",
        sa.Column("character_id", sa.String(length=36), primary_key=True),
        sa.Column("operator_id", sa.String(length=64), primary_key=True),
        sa.Column("note", sa.Text(), nullable=False),
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
    op.drop_table("player_persona_notes")
