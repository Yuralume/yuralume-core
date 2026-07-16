"""story arc template consumption bookkeeping

Revision ID: e5l7m2n30082
Revises: db3g5h80078
Create Date: 2026-06-04
"""

from __future__ import annotations

from datetime import datetime, timezone

from alembic import op
import sqlalchemy as sa


revision = "e5l7m2n30082"
down_revision = "db3g5h80078"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "story_arcs",
        sa.Column("source_template_id", sa.String(length=64), nullable=True),
    )

    bind = op.get_bind()
    story_arcs = sa.table(
        "story_arcs",
        sa.column("id", sa.String()),
        sa.column("character_id", sa.String()),
        sa.column("title", sa.Text()),
        sa.column("premise", sa.Text()),
        sa.column("status", sa.String()),
        sa.column("source_template_id", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    characters = sa.table(
        "characters",
        sa.column("id", sa.String()),
        sa.column("arc_template_id", sa.String()),
    )

    rows = bind.execute(
        sa.select(
            story_arcs.c.id,
            story_arcs.c.character_id,
            story_arcs.c.title,
            story_arcs.c.premise,
            story_arcs.c.status,
            characters.c.arc_template_id,
        )
        .select_from(
            story_arcs.join(
                characters,
                story_arcs.c.character_id == characters.c.id,
            ),
        )
        .where(
            story_arcs.c.source_template_id.is_(None),
            characters.c.arc_template_id.is_not(None),
            story_arcs.c.status.in_(("completed", "active")),
        ),
    ).all()

    completed_keys = {
        (row.character_id, row.title, row.premise)
        for row in rows
        if row.status == "completed"
    }
    now = datetime.now(timezone.utc)
    for row in rows:
        values: dict[str, object] = {}
        if row.status == "completed":
            values["source_template_id"] = row.arc_template_id
        elif (row.character_id, row.title, row.premise) in completed_keys:
            values["source_template_id"] = row.arc_template_id
            values["status"] = "completed"
            values["updated_at"] = now
        if not values:
            continue
        bind.execute(
            story_arcs.update()
            .where(story_arcs.c.id == row.id)
            .values(**values),
        )


def downgrade() -> None:
    op.drop_column("story_arcs", "source_template_id")
