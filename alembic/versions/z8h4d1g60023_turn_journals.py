"""add turn_journals table

Per-turn rollback records. Stores the pre-turn snapshots needed to
reverse one chat turn: conversation truncation back to ``turn_index``,
state/goal/arc/schedule restore, and time-window deletion of the
memory + state_snapshot rows the turn added. Keeps at most 5 per
conversation (enforced in the service layer after each write); FK
cascade to ``conversations`` cleans up when a conversation is removed.

(Correction, 2026-08-25 / TU1: this docstring used to claim the undo
also deleted ``story_event`` rows and that the payload carried
``added_*`` id lists. Neither was ever implemented — the deletes are
time-window based off ``turn_started_at``, and story events were left
alone. Story-event rollback is TU6's job, not this migration's.)

Revision ID: z8h4d1g60023
Revises: y7g3c0f50022
Create Date: 2026-04-24 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "z8h4d1g60023"
down_revision: Union[str, None] = "y7g3c0f50022"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "turn_journals",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "conversation_id", sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("turn_index", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column("payload_json", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_turn_journals_conversation_id",
        "turn_journals", ["conversation_id"],
    )
    op.create_index(
        "ix_turn_journals_character_id",
        "turn_journals", ["character_id"],
    )
    op.create_index(
        "ix_turn_journals_created_at",
        "turn_journals", ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_turn_journals_created_at", table_name="turn_journals")
    op.drop_index("ix_turn_journals_character_id", table_name="turn_journals")
    op.drop_index("ix_turn_journals_conversation_id", table_name="turn_journals")
    op.drop_table("turn_journals")
