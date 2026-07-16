"""operator_profiles.primary_language NOT NULL

Backs FRONTEND_I18N_PLAN.md's two-layer locale design: every operator
gets a fixed primary language at registration time that anchors all
LLM content (chat, memory, persona, story, feed) for their lifetime.
The frontend UI locale switcher is independent and may differ.

Why immutable in product: chat history, operator persona, disposition,
relationships etc. are accreted in whichever language the operator
first used. Swapping ``primary_language`` later would force the LLM to
straddle two languages in the same memory layer, which breaks
cross-time retrieval and narrative coherence. The migration enforces
NOT NULL with a backfill default of ``zh-TW`` so historical rows match
the project's TW-first heritage; no row in production today was
created against another language.

Schema-level we don't add CHECK constraints — BCP 47 validation lives
on the domain entity (``normalise_language_tag``) where it can raise
``ValueError`` with a useful message. The column is just String(16)
because BCP 47 tags top out well under that.

Revision ID: cu6z8a10071
Revises: ct5y7z00070
Create Date: 2026-05-22 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cu6z8a10071"
down_revision: Union[str, None] = "ct5y7z00070"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_LANGUAGE = "zh-TW"


def upgrade() -> None:
    # Three-step pattern (add nullable → backfill → enforce NOT NULL)
    # used elsewhere in this repo for any column that must be NOT NULL
    # against existing data. SQLite can't ALTER NOT NULL in-place; batch
    # mode rebuilds the table, Postgres handles it natively.
    with op.batch_alter_table("operator_profiles") as batch_op:
        batch_op.add_column(
            sa.Column(
                "primary_language",
                sa.String(length=16),
                nullable=True,
                server_default=DEFAULT_LANGUAGE,
            ),
        )

    operator_profiles = sa.table(
        "operator_profiles",
        sa.column("primary_language", sa.String),
    )
    op.execute(
        operator_profiles.update()
        .where(operator_profiles.c.primary_language.is_(None))
        .values(primary_language=DEFAULT_LANGUAGE)
    )

    with op.batch_alter_table("operator_profiles") as batch_op:
        batch_op.alter_column(
            "primary_language",
            existing_type=sa.String(length=16),
            nullable=False,
            existing_server_default=DEFAULT_LANGUAGE,
        )


def downgrade() -> None:
    with op.batch_alter_table("operator_profiles") as batch_op:
        batch_op.drop_column("primary_language")
