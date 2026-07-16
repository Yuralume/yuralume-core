"""add conversations.source

Channels (TG / LINE / ...) use their own conversation thread; the web
UI's "latest conversation" lookup filters by source so messaging
activity doesn't hijack the panel. Column is NOT NULL with default
``"web"``; already-bound conversations get backfilled with the binding's
platform so the filter is accurate from day one.

Revision ID: k2e9a6d50008
Revises: j1d8f5c40007
Create Date: 2026-04-18 17:45:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "k2e9a6d50008"
down_revision: Union[str, None] = "j1d8f5c40007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="web",
        ),
    )
    op.create_index(
        "ix_conversations_source",
        "conversations",
        ["source"],
    )
    # Backfill: conversations already pointed at by a channel binding
    # should carry that binding's platform as their source — otherwise
    # the web-UI filter would still surface TG/LINE threads that
    # pre-dated this migration.
    op.execute(
        """
        UPDATE conversations
        SET source = cb.platform
        FROM channel_bindings cb
        WHERE cb.conversation_id = conversations.id
        """,
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_source", table_name="conversations")
    op.drop_column("conversations", "source")
