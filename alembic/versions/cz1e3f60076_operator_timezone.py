"""operator_profiles.timezone_id

Revision ID: cz1e3f60076
Revises: cy0d2e50075
Create Date: 2026-05-27 15:00:00.000000
"""

from typing import Sequence, Union
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from alembic import op
import sqlalchemy as sa


revision: str = "cz1e3f60076"
down_revision: Union[str, None] = "cy0d2e50075"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    default_tz = _migration_default_timezone()
    with op.batch_alter_table("operator_profiles") as batch_op:
        batch_op.add_column(
            sa.Column(
                "timezone_id",
                sa.String(length=64),
                nullable=True,
                server_default="UTC",
            ),
        )

    profiles = sa.table(
        "operator_profiles",
        sa.column("timezone_id", sa.String),
    )
    op.execute(
        profiles.update()
        .where(profiles.c.timezone_id.is_(None))
        .values(timezone_id=default_tz),
    )

    with op.batch_alter_table("operator_profiles") as batch_op:
        batch_op.alter_column(
            "timezone_id",
            existing_type=sa.String(length=64),
            nullable=False,
            server_default="UTC",
        )


def downgrade() -> None:
    with op.batch_alter_table("operator_profiles") as batch_op:
        batch_op.drop_column("timezone_id")


def _migration_default_timezone() -> str:
    raw = (
        os.getenv("USER_TIMEZONE", "")
        or os.getenv("KOKORO_USER_TIMEZONE", "")
        or "UTC"
    )
    value = raw.strip()
    if not value or value.upper() == "UTC":
        return "UTC"
    if value.lower() in {"local", "server-local", "system", "os"}:
        return "UTC"
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError:
        return "UTC"
    return value
