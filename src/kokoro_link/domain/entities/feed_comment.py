"""Feed-wall comment entity.

A ``FeedComment`` is one user-authored comment on a feed post. Phase A2
only models user → character comments (replies / threading land later
when the character itself can comment back). The character "sees" new
comments via the A3 ``reactions_seen_at`` flow; this layer just owns
the persistence contract.

Single-user local app today: ``author_id`` defaults to
``LOCAL_COMMENTER_ID`` (mirrors ``LOCAL_LIKER_ID``) so the frontend
doesn't plumb identity. The column is free-form text so multi-user /
messaging-bot comments slot in later without a migration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


LOCAL_COMMENTER_ID = "local"
"""Default identity stamped on every comment in single-user mode."""

_MAX_COMMENT_CHARS = 2000
"""Hard ceiling on a single comment body. Anything past this is almost
certainly a paste accident; reject loudly so we don't silently truncate
something the user expected to publish in full."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class FeedComment:
    """One ``post_id × author × created_at`` comment row.

    Frozen so call sites can pass it around safely; ``create`` is the
    canonical constructor that fills id / timestamp defaults and trims
    the body.
    """

    id: str
    post_id: str
    author_id: str
    content_text: str
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("FeedComment.id must be non-empty")
        if not self.post_id:
            raise ValueError("FeedComment.post_id must be non-empty")
        if not self.author_id or not self.author_id.strip():
            raise ValueError("FeedComment.author_id must be non-empty")
        body = (self.content_text or "").strip()
        if not body:
            raise ValueError("FeedComment.content_text must be non-empty")
        if len(body) > _MAX_COMMENT_CHARS:
            raise ValueError(
                "FeedComment.content_text exceeds "
                f"{_MAX_COMMENT_CHARS} characters",
            )
        object.__setattr__(self, "author_id", self.author_id.strip())
        object.__setattr__(self, "content_text", body)

    @classmethod
    def create(
        cls,
        *,
        post_id: str,
        content_text: str,
        author_id: str = LOCAL_COMMENTER_ID,
        created_at: datetime | None = None,
        id: str | None = None,
    ) -> "FeedComment":
        return cls(
            id=id or uuid4().hex,
            post_id=post_id,
            author_id=author_id,
            content_text=content_text,
            created_at=created_at or _utcnow(),
        )
