"""SQLAlchemy rows for fusion-story persistence.

Three tables:

- ``fusion_stories``           — head row (current title / premise / status / full_text)
- ``fusion_story_beats``       — current beats for the head version
- ``fusion_story_versions``    — append-only chain of prior heads for rollback / diff

Beats are rebuilt atomically on each ``save`` (mirrors the
``story_arcs`` / ``story_arc_beats`` pattern); versions are append-only
and never modified after insert. Cascade deletes wipe both children
when the head row is removed.

Lives in its own module — the main ``models.py`` is already approaching
the per-file size guideline and adding three more rows here would push
it over.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from kokoro_link.infrastructure.persistence.models import Base


class FusionStoryRow(Base):
    """Top-level fusion-story head row.

    ``character_ids_json`` is a JSON-encoded ordered list of character
    ids — small (≤5 entries) and only ever read in bulk with the row,
    so a join table wouldn't pay back the extra round-trip.

    ``status`` mirrors ``FusionStory.status``; the service layer drives
    transitions and never trusts a UI input for this column.
    """

    __tablename__ = "fusion_stories"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_ids_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    prompt: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    premise: Mapped[str] = mapped_column(Text, nullable=False)
    theme: Mapped[str] = mapped_column(Text, nullable=False, default="custom")
    outline_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}",
    )
    """Serialized ``FusionOutline`` snapshot for the current head.

    Beats live in ``fusion_story_beats``; this column carries the
    structural envelope (per-beat target_chars / hook / focus ids) so
    the service can reconstruct a ``FusionOutline`` without reverse-
    engineering it from beat rows."""
    full_text: Mapped[str] = mapped_column(
        Text, nullable=False, default="",
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="planning", index=True,
    )
    head_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1,
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )


class FusionStoryBeatRow(Base):
    """Current per-beat prose for the head version.

    Rebuilt atomically on every ``save`` (the beat set is small —
    always exactly 4 rows in practice — so delete-all + re-insert is
    cheaper to reason about than per-beat diffing)."""

    __tablename__ = "fusion_story_beats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    story_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("fusion_stories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    act: Mapped[str] = mapped_column(
        String(32), nullable=False, default="opening",
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    hook: Mapped[str] = mapped_column(Text, nullable=False, default="")
    dramatic_question: Mapped[str] = mapped_column(
        Text, nullable=False, default="",
    )
    target_chars: Mapped[int] = mapped_column(
        Integer, nullable=False, default=600,
    )
    actual_chars: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    focus_character_ids_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )


class FusionStoryVersionRow(Base):
    """Append-only history of prior head versions.

    Each iterate operation snapshots the prior head into one of these
    rows. Never updated after insert — the version chain is the
    operator's audit trail.
    """

    __tablename__ = "fusion_story_versions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    story_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("fusion_stories.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    premise: Mapped[str] = mapped_column(Text, nullable=False)
    theme: Mapped[str] = mapped_column(Text, nullable=False, default="custom")
    full_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    outline_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}",
    )
    iteration_label: Mapped[str] = mapped_column(
        String(64), nullable=False, default="iterate",
    )
    beats_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
