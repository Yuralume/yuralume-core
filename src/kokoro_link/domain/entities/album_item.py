"""Character album entry.

Separate from ``Character.image_urls`` (stage carousel, capped at 12)
because the album's job is different: it's a long-tail archive of
images the character has accumulated — auto-collected from tool
generations + hand-transferred from the stage. No hard UX cap, but
we enforce a sanity ceiling at the service layer so a runaway tool
loop can't fill the disk.

The file itself lives wherever it was originally written
(``/uploads/characters/{id}/tools/{uuid}.png`` for tool output,
``/uploads/characters/{id}/{name}.{ext}`` for stage originals). The
album row is just an index — moving an image between stage and album
flips which table references it, the file stays put.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


SOURCE_TOOL = "tool"
"""Auto-collected from a tool invocation (ComfyUI etc.)."""
SOURCE_STAGE = "stage"
"""Operator transferred this from the stage carousel."""
SOURCE_UPLOAD = "upload"
"""Direct upload into the album (future use — not wired yet)."""
SOURCE_CANDIDATES = "candidates"
"""Operator committed this directly from a gacha candidate batch,
skipping the stage carousel."""

_VALID_SOURCES = frozenset(
    {SOURCE_TOOL, SOURCE_STAGE, SOURCE_UPLOAD, SOURCE_CANDIDATES},
)


@dataclass(frozen=True, slots=True)
class AlbumItem:
    id: str
    character_id: str
    url: str
    """Server-relative URL (``/uploads/...``) — same shape as
    ``Character.image_urls`` entries so the frontend renders them the
    same way."""
    source: str
    caption: str | None = None
    byte_size: int | None = None
    """Informational; used by the UI for ``3.2 MB`` annotations. Can be
    ``None`` when the writer didn't know (e.g. legacy backfills)."""
    created_at: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    def __post_init__(self) -> None:
        if not self.character_id:
            raise ValueError("character_id is required")
        if not self.url:
            raise ValueError("url is required")
        if self.source not in _VALID_SOURCES:
            raise ValueError(
                f"source {self.source!r} must be one of "
                f"{sorted(_VALID_SOURCES)}",
            )
        if self.created_at.tzinfo is None:
            raise ValueError("created_at must be timezone-aware")
        if self.byte_size is not None and self.byte_size < 0:
            raise ValueError("byte_size must be >= 0")

    @classmethod
    def create(
        cls,
        *,
        character_id: str,
        url: str,
        source: str,
        caption: str | None = None,
        byte_size: int | None = None,
        created_at: datetime | None = None,
    ) -> "AlbumItem":
        """Factory that assigns a fresh uuid + defaults ``created_at``.

        Prefer this over the raw constructor in services / tests —
        constructor is kept open for repo-side reconstruction from DB
        rows where ids and timestamps already exist.
        """
        return cls(
            id=uuid4().hex,
            character_id=character_id,
            url=url,
            source=source,
            caption=caption,
            byte_size=byte_size,
            created_at=created_at or datetime.now(timezone.utc),
        )
