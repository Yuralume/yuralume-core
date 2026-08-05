"""story_scene_sessions — DB-backed state for player-pulled scenes (SC1-A).

「起幕」 turns a stretch of the ordinary chat thread into a framed scene.
Everything a later request needs to know about that scene — which layer
of the §3.1 waterfall supplied the material, which beat is being played,
what the frame says, when the player last acted — has to be readable by a
*different* replica than the one that opened it: the hosted topology
load-balances 2× api, and the SC1-E timeout closer runs in the worker.
So the scene lives in this table and nowhere else (the 2026-07-26
cross-instance sweep is exactly the "it was a process attribute" bug).

``uq_story_scene_sessions_open_character`` is the invariant that matters:
a *partial* unique index over ``character_id`` restricted to
``status = 'open'``, the same portable shape ``story_arcs`` uses for its
single-active-arc rule (PostgreSQL and SQLite both support partial
indexes). Closed sessions are unconstrained, so a character accumulates
its full history of played scenes. Two taps of 起幕 landing on two
replicas is a plain race that no read-then-write can close; here the
second INSERT simply fails and the service reports "場景進行中".

``arc_id`` / ``beat_id`` are deliberately **not** foreign keys. The arc
repository's ``save`` rewrites a whole arc's beat rows as a unit, so an
FK with ``ON DELETE SET NULL`` would quietly unlink a live scene from its
beat on the next unrelated arc edit — the same reasoning that keeps
``story_arc_beats.realized_event_id`` FK-free.

Revision ID: b4s1m8p10030
Revises: a3r0l7o10029
Create Date: 2026-08-01
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "b4s1m8p10030"
down_revision = "a3r0l7o10029"
branch_labels = None
depends_on = None


_TABLE = "story_scene_sessions"
_OPEN_INDEX = "uq_story_scene_sessions_open_character"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "character_id",
            sa.String(length=36),
            sa.ForeignKey("characters.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "conversation_id",
            sa.String(length=36),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("source_layer", sa.String(length=24), nullable=False),
        sa.Column("arc_id", sa.String(length=36), nullable=True),
        sa.Column("beat_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("location", sa.Text(), nullable=True),
        sa.Column("mood", sa.String(length=64), nullable=True),
        sa.Column("scene_type", sa.String(length=32), nullable=True),
        sa.Column("dramatic_question", sa.Text(), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "last_activity_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_reason", sa.String(length=24), nullable=True),
    )
    op.create_index(
        _OPEN_INDEX,
        _TABLE,
        ["character_id"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
        sqlite_where=sa.text("status = 'open'"),
    )
    # The SC1-E closer sweeps "open and idle since X" across every
    # character, so it reads this pair and never the character column.
    op.create_index(
        "ix_story_scene_sessions_status_activity",
        _TABLE,
        ["status", "last_activity_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_story_scene_sessions_status_activity", table_name=_TABLE)
    op.drop_index(_OPEN_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
