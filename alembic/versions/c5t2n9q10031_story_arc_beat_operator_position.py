"""story_arc_beats — give the player a structured slot in a scene (OP0-A).

A beat could name its NPC cast (``scene_characters``) and its stakes
(``dramatic_question``) but had nowhere to say where the *player* stands
in the scene. Every consumer therefore re-invented that from prose on
each turn — the structural cause of the "玩家沒位置、對話硬塞" the
ARC_PLAYER_POSITION_PLAN diagnoses.

Two additive nullable columns:

- ``operator_position`` — ``VARCHAR(16)``, closed vocabulary
  (``absent`` / ``present`` / ``central``) validated in the domain.
- ``operator_note`` — ``TEXT``, optional prose ("她要向你坦白").

**No backfill, no server default.** ``NULL`` is not a missing value here:
it is the domain's fourth state, *unjudged*, and it is the correct thing
to say about every beat that existed before this column. Guessing a
position for stock rows would be inventing story facts — the plan's
zero-migration-of-existing-data red line (§4 #2) exists precisely so the
consumers answer ``NULL`` by reasoning about the beat instead of trusting
a value nobody authored.

No index either: the columns are only ever read with the beat row they
belong to (same reasoning as ``weather_vet_*`` / the SC0 failure
counters).

Revision ID: c5t2n9q10031
Revises: b4s1m8p10030
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "c5t2n9q10031"
down_revision = "b4s1m8p10030"
branch_labels = None
depends_on = None


_TABLE = "story_arc_beats"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("operator_position", sa.String(length=16), nullable=True),
    )
    op.add_column(
        _TABLE,
        sa.Column("operator_note", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column(_TABLE, "operator_note")
    op.drop_column(_TABLE, "operator_position")
