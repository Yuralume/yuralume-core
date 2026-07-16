"""message created_at

Revision ID: cf1k3l80056
Revises: ce0j2k70055
Create Date: 2026-05-19 12:00:00.000000

Add a per-Message timestamp so the conversation repo can merge messages
across sources (web / telegram / line / …) into a single chronological
history. Until now each ``Conversation`` lived in its own per-source
silo, which made the same character behave like a different person on
each channel — picking up the conversation on web after a TG exchange
left the LLM blind to everything that happened on TG. Cross-source
merging is keyed on this column.

Existing rows back-fill to ``CURRENT_TIMESTAMP`` at apply time. That's
not historically faithful but it preserves *per-conversation* ordering
(MessageRow.position is the secondary sort) and only affects pre-
existing conversations; new turns get real timestamps from the moment
the migration lands.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cf1k3l80056"
down_revision: Union[str, None] = "ce0j2k70055"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_messages_created_at",
        "messages",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_messages_created_at", table_name="messages")
    op.drop_column("messages", "created_at")
