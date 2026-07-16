"""arc_templates table — per-user authorship + bundled pack rows

Story arc templates previously lived as YAML files in
``src/kokoro_link/data/arc_templates/``. Containerised builds can't
write to that directory, and there's no per-user authorship — every
intake save would clobber the shared on-disk pack.

This migration moves templates into a regular DB table:

- Pack templates (shipped with the repo) carry ``user_id IS NULL``
  and are upserted from YAML on startup by ``ArcTemplatePackSyncService``.
- User-authored templates carry ``user_id = <owner>`` and are written
  by the intake wizard's save endpoint.
- ``Character.arc_template_id`` keeps referencing the string slug
  ``arc_templates.id``; ownership is enforced at the repository layer
  (visible = pack OR owner).

Revision ID: cy0d2e50075
Revises: cx9c1d40074
Create Date: 2026-05-26 12:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "cy0d2e50075"
down_revision: Union[str, None] = "cx9c1d40074"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "arc_templates",
        # The string slug ``Character.arc_template_id`` already references
        # (e.g. ``cafe_idol_audition``). Pack slugs and user-authored
        # slugs share this single namespace so a user who picks a slug
        # already taken by a pack gets 409, and lookup stays a single
        # primary-key fetch.
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=64),
            sa.ForeignKey("operator_profiles.id", ondelete="CASCADE"),
            nullable=True,
        ),
        # ``pack_id`` is the source YAML filename stem (e.g.
        # ``cafe_idol_audition``). Set on pack rows so the startup sync
        # can match DB rows back to disk files; NULL on user-authored
        # rows. We don't FK this anywhere — it's purely an audit hint.
        sa.Column("pack_id", sa.String(length=128), nullable=True),
        # The original ``id`` field declared inside the YAML, which may
        # differ from the file stem if the author overrode it. Kept for
        # provenance — running the pack sync recomputes the row but
        # this column makes "which YAML produced this row" answerable
        # without re-reading disk.
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("premise", sa.Text(), nullable=False),
        sa.Column(
            "theme",
            sa.String(length=64),
            nullable=False,
            server_default="custom",
        ),
        sa.Column(
            "tone",
            sa.String(length=64),
            nullable=False,
            server_default="daily",
        ),
        sa.Column(
            "duration_days",
            sa.Integer(),
            nullable=False,
            server_default="14",
        ),
        sa.Column(
            "world_frames_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "required_traits_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "beats_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        # ``enabled`` lets an admin hide a stale pack row without
        # deleting it (so the row keeps its history / external_id).
        # Disabled rows are excluded from list queries.
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
        ),
    )
    # Composite "list visible templates for user X" — pack rows (NULL
    # user_id) and the caller's own rows. Postgres uses the index to
    # serve both branches of the OR-IS-NULL query.
    op.create_index(
        "ix_arc_templates_user_id",
        "arc_templates",
        ["user_id"],
    )
    # Lookup by pack id during the startup sync. Filename stems are
    # short and unique so a btree on this column is enough.
    op.create_index(
        "ix_arc_templates_pack_id",
        "arc_templates",
        ["pack_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_arc_templates_pack_id", table_name="arc_templates")
    op.drop_index("ix_arc_templates_user_id", table_name="arc_templates")
    op.drop_table("arc_templates")
