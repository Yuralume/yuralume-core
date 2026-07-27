"""Conversation concurrency guards (B1 + B3).

B3: ``conversations.version`` is an optimistic-concurrency counter bumped under
a ``FOR UPDATE`` lock by every ``save()`` replace and every CAS
``append_messages``, so a stale whole-conversation ``save()`` can detect (and
merge, never silently drop) tail rows a concurrent append added. The
``uq_messages_conversation_position`` unique constraint makes a duplicated
position a hard DB error instead of a silent lost update.

B1: ``messages.idempotency_key`` is a turn-scoped key for a durable
single-message append. The ``uq_messages_conversation_idempotency`` unique
constraint treats NULLs as distinct (so ordinary un-keyed appends never
conflict) while making a keyed re-append idempotent — closing the crash window
between a durable user-turn append and the receipt ``record_user_turn`` so a
takeover adopts the original row instead of writing a duplicate.

Both are additive and backward compatible: existing rows get ``version = 1`` and
``idempotency_key = NULL``; existing ``(conversation_id, position)`` pairs are
already unique by construction.

Both uniqueness guards are created as UNIQUE INDEXES rather than
``ALTER TABLE ... ADD CONSTRAINT``: SQLite cannot add a table-level constraint
after creation without a full batch table-rebuild, whereas ``CREATE UNIQUE
INDEX`` is supported natively on both SQLite and PostgreSQL and is semantically
equivalent (a duplicate is a hard DB error; NULLs stay distinct so ordinary
un-keyed appends never conflict). This keeps the migration runnable on a clean
SQLite database without batch mode.

Revision ID: p1n8o3q4017
Revises: o0m7n2p3016
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "p1n8o3q4017"
down_revision = "o0m7n2p3016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default="1",
        ),
    )
    op.add_column(
        "messages",
        sa.Column("idempotency_key", sa.String(length=96), nullable=True),
    )
    # UNIQUE INDEX (not ADD CONSTRAINT) so a clean SQLite upgrade never needs
    # batch mode; on PostgreSQL a unique index is the same guarantee.
    op.create_index(
        "uq_messages_conversation_position",
        "messages",
        ["conversation_id", "position"],
        unique=True,
    )
    op.create_index(
        "uq_messages_conversation_idempotency",
        "messages",
        ["conversation_id", "idempotency_key"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_messages_conversation_idempotency", table_name="messages",
    )
    op.drop_index(
        "uq_messages_conversation_position", table_name="messages",
    )
    op.drop_column("messages", "idempotency_key")
    op.drop_column("conversations", "version")
