"""SQLAlchemy rows for branching-drama persistence.

Three tables:

- ``branching_dramas``          — top-level metadata + generation status
- ``branching_drama_nodes``     — tree nodes (one per segment variant)
- ``branching_drama_sessions``  — player playthrough state + turn history

Nodes are append-only during generation and only updated when image
paths are set. Sessions carry turn history in a JSON column (bounded
by total_segments — never more than ~10 entries per session).
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from kokoro_link.infrastructure.persistence.models import Base


class BranchingDramaRow(Base):
    __tablename__ = "branching_dramas"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_ids_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    total_segments: Mapped[int] = mapped_column(
        Integer, nullable=False, default=6,
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="generating_outlines",
        index=True,
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    """Gateway refusal code behind a ``failed`` status (U4b) — see
    ``FusionStoryRow.error_code``. ``NULL`` for ordinary crashes."""
    operator_position: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
    )
    """Where the player stands in this drama (BD2). Nullable with no
    server default and no backfill: ``NULL`` is what every pre-BD2 row
    says, and the mapper reads it back as
    ``DEFAULT_OPERATOR_POSITION`` so those dramas keep playing exactly
    as they did. No index — only ever read with the drama row."""
    operator_note: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    """Optional free-text 「你在劇中演誰」 (BD2). In-story staging only."""
    visual_style: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
    )
    """The look every scene image of this drama is drawn in (BD10).

    Nullable with no server default and no backfill — but unlike
    ``operator_position`` the ``NULL`` here is *load-bearing*: the mapper
    does **not** substitute a default, because a pre-BD10 drama was styled
    off its first character and reading it back as ``anime`` would silently
    repaint half the existing realistic ones. ``NULL`` routes the scene
    prompt to that legacy per-character resolution instead. No index —
    only ever read with the drama row."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )


class BranchingDramaNodeRow(Base):
    __tablename__ = "branching_drama_nodes"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    drama_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("branching_dramas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    parent_node_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True,
    )
    depth: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    tone: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    appearing_character_ids_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    image_path: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )


class BranchingDramaSessionRow(Base):
    __tablename__ = "branching_drama_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    drama_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("branching_dramas.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    current_node_id: Mapped[str] = mapped_column(
        String(36), nullable=False,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="playing",
    )
    turns_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
