"""default production tools and remove fake tool grants

Revision ID: cx9c1d40074
Revises: cw8b0c30073
Create Date: 2026-05-25 10:30:00.000000

Characters now default to the production tool catalogue instead of an
empty allow-list. Test-only tools (echo / fake_image) remain importable
for unit tests but should not survive in persisted character settings.
"""

from __future__ import annotations

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cx9c1d40074"
down_revision: Union[str, None] = "cw8b0c30073"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


DEFAULT_ALLOWED_TOOLS = ["generate_image", "web_fetch", "web_search"]
REMOVED_TOOLS = {"echo", "fake_image"}
DEFAULT_JSON = json.dumps(DEFAULT_ALLOWED_TOOLS, separators=(",", ":"))


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, allowed_tools FROM characters"),
    ).mappings()
    for row in rows:
        raw = row["allowed_tools"] or "[]"
        try:
            current = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            current = []
        if not isinstance(current, list):
            current = []

        cleaned = [
            value
            for value in current
            if isinstance(value, str) and value not in REMOVED_TOOLS
        ]
        next_tools = cleaned[:]
        for name in DEFAULT_ALLOWED_TOOLS:
            if name not in next_tools:
                next_tools.append(name)
        if next_tools != current:
            bind.execute(
                sa.text(
                    "UPDATE characters SET allowed_tools = :tools WHERE id = :id",
                ),
                {
                    "id": row["id"],
                    "tools": json.dumps(next_tools, ensure_ascii=False),
                },
            )

    with op.batch_alter_table("characters") as batch_op:
        batch_op.alter_column(
            "allowed_tools",
            existing_type=sa.Text(),
            server_default=sa.text(f"'{DEFAULT_JSON}'"),
            existing_nullable=False,
        )


def downgrade() -> None:
    with op.batch_alter_table("characters") as batch_op:
        batch_op.alter_column(
            "allowed_tools",
            existing_type=sa.Text(),
            server_default=sa.text("'[]'"),
            existing_nullable=False,
        )
