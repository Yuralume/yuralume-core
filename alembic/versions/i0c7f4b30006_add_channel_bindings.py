"""add channel_bindings table

Slice 0 of the messaging-channels feature. One row per
(platform, chat_ref) pair, binding that chat to a character so inbound
messages route to the right conversation and replies go back through
the matching adapter.

Two unique indexes enforce the product invariants (one chat -> one
character, one character -> one chat per platform). FK to ``characters``
cascades so deleting a character cleans its bindings.

Revision ID: i0c7f4b30006
Revises: h9b6e3a20005
Create Date: 2026-04-18 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "i0c7f4b30006"
down_revision: Union[str, None] = "h9b6e3a20005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "channel_bindings",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("platform", sa.String(length=32), nullable=False, index=True),
        sa.Column("chat_ref", sa.String(length=128), nullable=False),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "platform", "chat_ref",
            name="uq_channel_bindings_platform_chat",
        ),
        sa.UniqueConstraint(
            "platform", "character_id",
            name="uq_channel_bindings_platform_character",
        ),
    )


def downgrade() -> None:
    op.drop_table("channel_bindings")
