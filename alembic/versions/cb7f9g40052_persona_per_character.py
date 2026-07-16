"""operator_profile_fields → per-character persona

Pivots the persona table from operator-scoped (one row per layer/key
shared by every character) to ``(character_id, operator_id)``-scoped
(each character builds their own picture of the operator).

Why: a shared persona meant a brand-new character magically "knew"
everything the operator had ever told previous characters — that
broke the "stranger → acquaintance" arc which was the whole point of
the feature. Real social mirror: when you meet someone new, they
start from zero observations regardless of how well your old friends
know you.

Migration shape:

- ``ca6e8f30051`` shipped earlier today (2026-05-17) and applied
  cleanly, but no extraction has run yet (table is empty). We
  ``TRUNCATE`` defensively before adding the NOT-NULL FK so deploys
  that did manage to land a stray row don't crash the upgrade — those
  rows would have been unassignable to a character anyway.
- Adds ``character_id`` (NOT NULL, FK→characters.id with CASCADE so
  deleting a character also drops their persona observations, mirror
  of how memories cascade).
- Swaps the unique constraint from ``(operator_id, layer, field_key,
  state)`` to ``(character_id, operator_id, layer, field_key, state)``.
- Adds an index on ``character_id`` since the prompt-render path
  always filters by it first.

Revision ID: cb7f9g40052
Revises: ca6e8f30051
Create Date: 2026-05-17 14:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cb7f9g40052"
down_revision: Union[str, None] = "ca6e8f30051"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Defensive truncate — see module docstring.
    op.execute("TRUNCATE TABLE operator_profile_fields")
    op.add_column(
        "operator_profile_fields",
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
        ),
    )
    op.create_index(
        "ix_operator_profile_fields_character_id",
        "operator_profile_fields",
        ["character_id"],
    )
    op.drop_constraint(
        "uq_operator_profile_fields_state",
        "operator_profile_fields",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_operator_profile_fields_per_character_state",
        "operator_profile_fields",
        ["character_id", "operator_id", "layer", "field_key", "state"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_operator_profile_fields_per_character_state",
        "operator_profile_fields",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_operator_profile_fields_state",
        "operator_profile_fields",
        ["operator_id", "layer", "field_key", "state"],
    )
    op.drop_index(
        "ix_operator_profile_fields_character_id",
        table_name="operator_profile_fields",
    )
    op.drop_column("operator_profile_fields", "character_id")
