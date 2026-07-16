"""ORM models for the RSS event pipeline.

Kept separate from ``models.py`` to limit blast radius — the pipeline
ships as one cohesive feature and migrations / repositories all want to
import these together.
"""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column

from kokoro_link.infrastructure.persistence.models import Base, MEMORY_EMBEDDING_DIM


class WorldEventRow(Base):
    """Global event pool row.

    The migration ``q8e5c2d10014_world_events`` predates this ORM model
    (the original implementation only had an in-memory repo). Schema is
    described there; the ``category`` column was added in the
    ``bs8w0x50043_rss_pipeline`` migration.
    """

    __tablename__ = "world_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    url: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default="news", server_default="news",
        index=True,
    )
    locale: Mapped[str | None] = mapped_column(
        String(16), nullable=True, index=True,
    )
    topic_tags: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(MEMORY_EMBEDDING_DIM), nullable=True,
    )


class RssSourceRow(Base):
    __tablename__ = "rss_sources"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    feed_url: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default="news", server_default="news",
        index=True,
    )
    locale: Mapped[str] = mapped_column(
        String(16), nullable=False, default="zh-TW", server_default="zh-TW",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    fetched_count_total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    default_for_categories: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]",
    )


class CharacterEventInboxRow(Base):
    __tablename__ = "character_event_inbox"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
    )
    world_event_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("world_events.id", ondelete="CASCADE"),
        nullable=False,
    )
    similarity: Mapped[float] = mapped_column(
        Float, nullable=False, default=0.0, server_default="0",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    claimed_by_surface: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
