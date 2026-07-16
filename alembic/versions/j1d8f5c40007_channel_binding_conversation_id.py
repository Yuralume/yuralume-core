"""add channel_bindings.conversation_id

Each binding now owns its own conversation thread — TG / LINE / web
don't share conversation history (only character state and long-term
memory stay global). ``conversation_id`` is nullable because the thread
is created lazily on the first inbound message for that binding.

``ON DELETE SET NULL`` on the FK so deleting a conversation (e.g. from a
character cascade) clears the pointer rather than wiping the binding.

Revision ID: j1d8f5c40007
Revises: i0c7f4b30006
Create Date: 2026-04-18 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "j1d8f5c40007"
down_revision: Union[str, None] = "i0c7f4b30006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "channel_bindings",
        sa.Column("conversation_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        "fk_channel_bindings_conversation_id",
        "channel_bindings",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_channel_bindings_conversation_id",
        "channel_bindings",
        type_="foreignkey",
    )
    op.drop_column("channel_bindings", "conversation_id")
