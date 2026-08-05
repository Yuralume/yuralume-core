"""story_scene_sessions — carry the player's place in the scene (OP2-D).

The closer (SC1-D) only ever reads the scene *session*, never the beat it
opened from — the same reasoning ``scene_type`` / ``dramatic_question``
already carry: a scene must keep framing the player coherently even if its
beat is edited, retired, or realized while the scene is still being
played. Without these two columns the closer would have no way to see the
``operator_position`` / ``operator_note`` OP0-A put on the beat, and the
wrap-up would fall back to guessing the player's place in the scene it is
supposed to already know.

Two additive nullable columns, same shape as ``story_arc_beats``':

- ``operator_position`` — ``VARCHAR(16)``, closed vocabulary
  (``absent`` / ``present`` / ``central``) validated in the domain.
- ``operator_note`` — ``TEXT``, optional prose.

**No backfill.** Every scene session that existed before this column has
no position to backfill from — the column simply did not exist yet when
those sessions were opened — so ``NULL`` (unjudged) is the only honest
value, matching the plan's zero-migration-of-existing-data red line
(§4 #2).

Revision ID: d6u3o0r10032
Revises: c5t2n9q10031
Create Date: 2026-08-02
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "d6u3o0r10032"
down_revision = "c5t2n9q10031"
branch_labels = None
depends_on = None


_TABLE = "story_scene_sessions"


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
