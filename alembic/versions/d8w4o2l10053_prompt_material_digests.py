"""prompt_material_digests — the digest a turn budgets for the next (DIGEST_OFFPATH-2).

The chat prompt's material digest used to be an aux-LLM call made inside
every turn, in front of the first token. DIGEST_OFFPATH-1 moved it to the
post-turn: the turn that just ended distils its own emotion events,
reflections, story material and feed posts into bullets, and the *next*
turn reads them instead of paying for them.

The handoff needs a durable home rather than process memory, and that is
what this table is. On hosted the post-turn is a worker job while chat is
served from the API process, so a process-local cache is written by one
process and read by another that never sees it — the digest would be
silently absent on exactly the deployment that most wants it.

One row per ``(character, operator)``; the pair **is** the primary key,
so nothing can leave two live digests for one relationship. Same shape,
and the same reasoning, as ``dialogue_checkpoints``.

Two columns exist to bound what a reader will accept:

* ``content_tolerance`` — the bullets were generated to a tolerance, and
  a reader rendering for a different one must treat the row as absent
  rather than move content across that boundary.
* ``updated_at`` — a row never expires on its own. The read side refuses
  anything past its max-age, so a player returning after a month gets the
  source blocks rather than a month-old summary presented as current.

No backfill: an absent row means "nothing budgeted yet", which is the
state every pair starts in and the state the read path has always
degraded to (render the source blocks). Applying this migration alone
therefore changes nothing on its own.

Revision ID: d8w4o2l10053
Revises: c7v3n1k10052
Create Date: 2026-08-27 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d8w4o2l10053"
down_revision: Union[str, None] = "c7v3n1k10052"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "prompt_material_digests",
        sa.Column("character_id", sa.String(length=36), nullable=False),
        sa.Column("operator_id", sa.String(length=64), nullable=False),
        sa.Column(
            "content_tolerance", sa.String(length=32), nullable=False,
        ),
        sa.Column("digest_json", sa.Text(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["character_id"], ["characters.id"], ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "character_id", "operator_id", name="pk_prompt_material_digests",
        ),
    )


def downgrade() -> None:
    op.drop_table("prompt_material_digests")
