"""introduce messaging_accounts; restructure channel_bindings around it

Two-layer model: credentials + platform identity now live on a per
(character, platform) ``messaging_accounts`` row, and each
``channel_bindings`` row is reduced to "this account spoke with this
chat, here is the thread".

The previous shape tied bindings directly to ``(platform, character_id)``
with credentials held at the process level (env vars). That made one
Yuralume instance = one bot per platform — wrong for the one-bot-
per-character pattern Telegram and LINE require.

Because we're still in beta and the previous ``channel_bindings`` table
only holds a handful of hand-crafted rows, this migration wipes that
table before restructuring rather than attempting a data migration.

Revision ID: l3f0b7e60009
Revises: k2e9a6d50008
Create Date: 2026-04-18 19:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "l3f0b7e60009"
down_revision: Union[str, None] = "k2e9a6d50008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Clear the legacy bindings so the column changes are safe.
    op.execute("DELETE FROM channel_bindings")

    # 2. Create the new messaging_accounts table.
    op.create_table(
        "messaging_accounts",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("platform", sa.String(length=32), nullable=False, index=True),
        sa.Column("display_name", sa.Text(), nullable=False, server_default=""),
        sa.Column("webhook_slug", sa.String(length=64), nullable=False, index=True),
        sa.Column(
            "credentials_json", sa.Text(), nullable=False, server_default="{}",
        ),
        sa.Column(
            "allowed_sender_refs_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "enabled", sa.Boolean(), nullable=False, server_default=sa.true(),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "platform", "character_id",
            name="uq_messaging_accounts_platform_character",
        ),
        sa.UniqueConstraint(
            "webhook_slug", name="uq_messaging_accounts_webhook_slug",
        ),
    )

    # 3. Drop the constraints/indexes that reference the old columns
    #    BEFORE dropping the columns themselves — Postgres requires this
    #    ordering unless we CASCADE, which we don't want here.
    op.drop_constraint(
        "uq_channel_bindings_platform_chat",
        "channel_bindings",
        type_="unique",
    )
    op.drop_constraint(
        "uq_channel_bindings_platform_character",
        "channel_bindings",
        type_="unique",
    )
    op.drop_index(
        "ix_channel_bindings_character_id", table_name="channel_bindings",
    )
    op.drop_index(
        "ix_channel_bindings_platform", table_name="channel_bindings",
    )

    # Dropping the column takes the inline character FK with it (FKs are
    # scoped to their column, so no manual DROP CONSTRAINT needed). The
    # ``conversation_id`` FK added in j1d8f5c40007 stays untouched.
    op.drop_column("channel_bindings", "character_id")
    op.drop_column("channel_bindings", "platform")

    # 4. Add the new account_id column + FK + unique index.
    op.add_column(
        "channel_bindings",
        sa.Column("account_id", sa.String(length=36), nullable=False),
    )
    op.create_index(
        "ix_channel_bindings_account_id",
        "channel_bindings",
        ["account_id"],
    )
    op.create_foreign_key(
        "fk_channel_bindings_account_id",
        "channel_bindings",
        "messaging_accounts",
        ["account_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_channel_bindings_account_chat",
        "channel_bindings",
        ["account_id", "chat_ref"],
    )


def downgrade() -> None:
    op.execute("DELETE FROM channel_bindings")
    op.drop_constraint(
        "uq_channel_bindings_account_chat",
        "channel_bindings",
        type_="unique",
    )
    op.drop_constraint(
        "fk_channel_bindings_account_id",
        "channel_bindings",
        type_="foreignkey",
    )
    op.drop_index(
        "ix_channel_bindings_account_id", table_name="channel_bindings",
    )
    op.drop_column("channel_bindings", "account_id")

    op.add_column(
        "channel_bindings",
        sa.Column("platform", sa.String(length=32), nullable=False),
    )
    op.add_column(
        "channel_bindings",
        sa.Column("character_id", sa.String(length=36), nullable=False),
    )
    op.create_index(
        "ix_channel_bindings_platform",
        "channel_bindings",
        ["platform"],
    )
    op.create_index(
        "ix_channel_bindings_character_id",
        "channel_bindings",
        ["character_id"],
    )
    op.create_foreign_key(
        None,
        "channel_bindings",
        "characters",
        ["character_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_channel_bindings_platform_chat",
        "channel_bindings",
        ["platform", "chat_ref"],
    )
    op.create_unique_constraint(
        "uq_channel_bindings_platform_character",
        "channel_bindings",
        ["platform", "character_id"],
    )

    op.drop_table("messaging_accounts")
