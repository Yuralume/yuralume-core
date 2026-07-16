"""users auth columns + characters.user_id owner FK

Batch 1 of MULTI_USER_AUTH_PLAN. Two coordinated changes:

1. ``operator_profiles`` (the existing user/operator master table) gets
   three auth columns — ``email`` (partial-unique), ``password_hash``,
   ``is_admin``. All nullable / default-false so existing rows remain
   valid; the default user keeps ``password_hash IS NULL`` so the
   front-end can detect "needs setup" and prompt for credentials on
   first login.

2. ``characters`` gets a ``user_id`` NOT NULL FK to
   ``operator_profiles.id`` with ON DELETE CASCADE — this is the owner
   anchor for per-user isolation. Backfilled to ``"default"`` so every
   existing character stays attached to the single-user install's
   default operator.

A default-operator row is also ensured at the top of upgrade so the FK
backfill can't dangle on a fresh install where no chat has yet
materialised the singleton.

Revision ID: ct5y7z00070
Revises: cs4x6y00069
Create Date: 2026-05-21 18:00:00.000000
"""
from typing import Sequence, Union
from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision: str = "ct5y7z00070"
down_revision: Union[str, None] = "cs4x6y00069"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_OPERATOR_ID = "default"
DEFAULT_OPERATOR_DISPLAY_NAME = "操作者"


def upgrade() -> None:
    bind = op.get_bind()

    # --- (A) operator_profiles auth columns ---------------------------
    with op.batch_alter_table("operator_profiles") as batch_op:
        batch_op.add_column(
            sa.Column("email", sa.String(length=255), nullable=True),
        )
        batch_op.add_column(
            sa.Column("password_hash", sa.String(length=255), nullable=True),
        )
        batch_op.add_column(
            sa.Column(
                "is_admin",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            ),
        )

    # Partial unique on email — multiple NULL rows allowed (only one
    # default user pre-setup, but defensive against future seeds).
    # SQLite supports partial indexes via the ``WHERE`` clause syntax in
    # CREATE INDEX; Postgres does too. ``sqlite_where`` is the alembic
    # knob that emits the WHERE on both backends.
    op.create_index(
        "ix_operator_profiles_email_unique",
        "operator_profiles",
        ["email"],
        unique=True,
        sqlite_where=sa.text("email IS NOT NULL"),
        postgresql_where=sa.text("email IS NOT NULL"),
    )

    # --- (B) Ensure default operator row exists -----------------------
    # Operators table may be empty on a fresh install where no chat
    # turn has triggered the lazy upsert. We need it before backfilling
    # characters.user_id.
    now = datetime.now(timezone.utc)
    operator_profiles = sa.table(
        "operator_profiles",
        sa.column("id", sa.String),
        sa.column("display_name", sa.String),
        sa.column("aliases_json", sa.Text),
        sa.column("pronouns", sa.String),
        sa.column("email", sa.String),
        sa.column("password_hash", sa.String),
        sa.column("is_admin", sa.Boolean),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    existing = bind.execute(
        sa.select(operator_profiles.c.id).where(
            operator_profiles.c.id == DEFAULT_OPERATOR_ID
        )
    ).first()
    if existing is None:
        bind.execute(
            operator_profiles.insert().values(
                id=DEFAULT_OPERATOR_ID,
                display_name=DEFAULT_OPERATOR_DISPLAY_NAME,
                aliases_json="[]",
                pronouns=None,
                email=None,
                password_hash=None,
                is_admin=True,  # single-user owner is admin by definition
                created_at=now,
                updated_at=now,
            )
        )
    else:
        # Existing default row predates the auth columns — mark it as
        # admin so the eventual setup flow assigns ownership cleanly.
        bind.execute(
            operator_profiles.update()
            .where(operator_profiles.c.id == DEFAULT_OPERATOR_ID)
            .values(is_admin=True)
        )

    # --- (C) characters.user_id owner FK ------------------------------
    # Three-step pattern (add nullable → backfill → enforce NOT NULL)
    # — same shape every previous owner-anchor migration in this repo
    # uses.
    with op.batch_alter_table("characters") as batch_op:
        batch_op.add_column(
            sa.Column(
                "user_id",
                sa.String(length=64),
                nullable=True,
            ),
        )

    characters = sa.table(
        "characters",
        sa.column("id", sa.String),
        sa.column("user_id", sa.String),
    )
    bind.execute(
        characters.update()
        .where(characters.c.user_id.is_(None))
        .values(user_id=DEFAULT_OPERATOR_ID)
    )

    # SQLite cannot ALTER COLUMN NOT NULL in-place; batch mode rebuilds
    # the table. Postgres handles it natively. Both paths add the FK in
    # the same batch.
    with op.batch_alter_table("characters") as batch_op:
        batch_op.alter_column("user_id", nullable=False)
        batch_op.create_foreign_key(
            "fk_characters_user_id",
            "operator_profiles",
            ["user_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.create_index(
            "ix_characters_user_id",
            ["user_id"],
        )


def downgrade() -> None:
    with op.batch_alter_table("characters") as batch_op:
        batch_op.drop_index("ix_characters_user_id")
        batch_op.drop_constraint("fk_characters_user_id", type_="foreignkey")
        batch_op.drop_column("user_id")

    op.drop_index(
        "ix_operator_profiles_email_unique",
        table_name="operator_profiles",
    )
    with op.batch_alter_table("operator_profiles") as batch_op:
        batch_op.drop_column("is_admin")
        batch_op.drop_column("password_hash")
        batch_op.drop_column("email")
