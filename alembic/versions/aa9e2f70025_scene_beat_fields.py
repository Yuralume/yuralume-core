"""scene-structure fields on story_arc_beats

Phase 1 of ``docs/SCENE_BEAT_PLAN.md`` — adds ``scene_characters``
(JSON-encoded list as Text), ``location``, ``dramatic_question``,
``scene_type``, ``required`` so a beat carries the structure needed
for the expander to compose a "play this scene" prompt instead of
re-summarising a paragraph. All columns get safe defaults so existing
arcs read back unchanged (``scene_type='encounter'``, ``required=true``,
empty list / NULL elsewhere).

Revision ID: aa9e2f70025
Revises: a9i5e2h70024
Create Date: 2026-04-26 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "aa9e2f70025"
down_revision: Union[str, None] = "a9i5e2h70024"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default carries existing rows through. After this
    # migration the application is the source of truth (the ORM model
    # only defines client-side defaults), so we drop the server_default
    # afterwards to keep INSERT semantics aligned with the model.
    op.add_column(
        "story_arc_beats",
        sa.Column(
            "scene_characters",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
    )
    op.add_column(
        "story_arc_beats",
        sa.Column("location", sa.Text(), nullable=True),
    )
    op.add_column(
        "story_arc_beats",
        sa.Column("dramatic_question", sa.Text(), nullable=True),
    )
    op.add_column(
        "story_arc_beats",
        sa.Column(
            "scene_type",
            sa.String(length=32),
            nullable=False,
            server_default="encounter",
        ),
    )
    op.add_column(
        "story_arc_beats",
        sa.Column(
            "required",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )

    # Drop server_default so future INSERTs flow through the ORM
    # client-side default (matches how other columns on this table
    # behave: ``tension``/``status`` use mapped_column default=...).
    op.alter_column("story_arc_beats", "scene_characters", server_default=None)
    op.alter_column("story_arc_beats", "scene_type", server_default=None)
    op.alter_column("story_arc_beats", "required", server_default=None)


def downgrade() -> None:
    op.drop_column("story_arc_beats", "required")
    op.drop_column("story_arc_beats", "scene_type")
    op.drop_column("story_arc_beats", "dramatic_question")
    op.drop_column("story_arc_beats", "location")
    op.drop_column("story_arc_beats", "scene_characters")
