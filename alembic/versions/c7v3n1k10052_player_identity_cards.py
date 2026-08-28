"""player_identity_cards — 玩家身分卡 (IC1 of PLAYER_IDENTITY_CARD_PLAN)

A named, reusable copy of the character-creation intake: the eleven
relationship-seed fields plus the player's persona note. The player saves
one once ("上班族的我") and starts the next character from it instead of a
blank wizard.

Operator-level by design — **no ``character_id`` column**. A card outlives
every character made from it, and a character-scoped column would also
enrol the player's whole card library into that character's
``.lumebackup`` (the character-boundary registry classifies tables by
exactly that column shape). Deleting a character therefore does not touch
cards, and deleting a card does not touch characters: applying a card
copies values, it does not link them.

``(operator_id, name)`` is unique. The name is the only handle the player
has in the picker, so re-saving under an existing name is an overwrite of
that row (same ``id``, bumped ``updated_at``), never a second row.

Revision ID: c7v3n1k10052
Revises: z5s2n9q80051
Create Date: 2026-08-27 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7v3n1k10052"
down_revision: Union[str, None] = "z5s2n9q80051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "player_identity_cards",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column(
            "relationship_label", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column("known_context", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "living_arrangement", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column(
            "user_address_name", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column(
            "character_address_name", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column("tone_distance", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "familiarity_boundary", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column(
            "schedule_involvement_policy",
            sa.String(length=32),
            nullable=False,
            server_default="none",
        ),
        sa.Column(
            "proactive_permission",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "proactive_cadence_hint", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column(
            "user_profile_notes", sa.Text(), nullable=False, server_default="",
        ),
        sa.Column("persona_note", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["operator_id"], ["operator_profiles.id"], ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "operator_id", "name", name="uq_player_identity_cards_operator_name",
        ),
    )
    op.create_index(
        "ix_player_identity_cards_operator",
        "player_identity_cards",
        ["operator_id", "updated_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_player_identity_cards_operator",
        table_name="player_identity_cards",
    )
    op.drop_table("player_identity_cards")
