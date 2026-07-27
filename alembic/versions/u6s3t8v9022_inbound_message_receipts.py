"""inbound_message_receipts — cross-instance inbound delivery dedup.

``InboundDebouncer`` keeps the last ``platform_message_id`` per
``(platform, chat_ref)`` in a *per-process* dict (its own docstring says
"fine for single-process deployments"). The Telegram / LINE webhook routes run
the complete chat turn — LLM call included — inside the request handler, so a
slow turn makes the platform time out and re-deliver. Under the hosted
multi-replica topology (and any multi-process self-host) the load balancer can
land that retry on a second api instance whose ``_seen`` dict is cold, and the
whole turn runs twice: two replies to the user, two charges.

This table is the durable claim that closes it. One row per delivery; the
unique index decides the race, so exactly one instance proceeds and every
other re-delivery drops on the pre-existing debounce path.

Key is ``(platform, account_id, chat_ref, platform_message_id)``. The account
dimension is load-bearing, not defensive: Telegram's parser mints
``"{chat_id}:{message_id}"`` and a private chat's ``chat.id`` is the *user's
own id* — identical across every bot that user talks to — so two accounts
bound to the same human produce colliding pairs. Retries of one delivery
always carry the same ``account_id`` (the route resolves the account from the
request path before building the ``InboundMessage``), so the extra dimension
cannot weaken the dedup.

Purely additive: no backfill, no FK. Receipts prune on a 7-day retention
clock (opportunistic DELETE on the claim path, at most hourly per process),
which the ``created_at`` index serves.

Revision ID: u6s3t8v9022
Revises: t5r2s7u8021
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "u6s3t8v9022"
down_revision = "t5r2s7u8021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "inbound_message_receipts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("platform", sa.String(length=32), nullable=False),
        sa.Column("account_id", sa.String(length=36), nullable=False),
        sa.Column("chat_ref", sa.String(length=128), nullable=False),
        sa.Column(
            "platform_message_id", sa.String(length=255), nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "platform", "account_id", "chat_ref", "platform_message_id",
            name="uq_inbound_message_receipts_delivery",
        ),
    )
    op.create_index(
        "ix_inbound_message_receipts_created",
        "inbound_message_receipts",
        ["created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_inbound_message_receipts_created",
        table_name="inbound_message_receipts",
    )
    op.drop_table("inbound_message_receipts")
