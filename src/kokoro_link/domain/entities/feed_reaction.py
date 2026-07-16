"""Feed-wall reaction (like) entity.

A ``FeedReaction`` is a single user's like on a single feed post.
Phase A1 only models likes; comments live in their own entity (A2).

Single-user local app today: ``liker_id`` defaults to the
``LOCAL_LIKER_ID`` constant so the frontend doesn't need to know about
identity. Forward-compat for multi-user / messaging-bot likes is built
in by keeping the column free-form.

Uniqueness rule: (post_id, liker_id) — a user can like a post at most
once. The repo enforces this so a double-tap from the UI is
idempotent rather than producing two rows.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4


LOCAL_LIKER_ID = "local"
"""Default identity stamped on every reaction in single-user mode.

Kept as a public module constant so callers (routes, tests, frontend
fixture data) reference one canonical string instead of repeating
``"local"`` literals."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True, slots=True)
class FeedReaction:
    """One ``post_id × liker_id`` like row.

    Frozen so call sites can pass it around safely; ``create`` is the
    canonical constructor that fills id / timestamp defaults.
    """

    id: str
    post_id: str
    liker_id: str
    created_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("FeedReaction.id must be non-empty")
        if not self.post_id:
            raise ValueError("FeedReaction.post_id must be non-empty")
        if not self.liker_id or not self.liker_id.strip():
            raise ValueError("FeedReaction.liker_id must be non-empty")
        object.__setattr__(self, "liker_id", self.liker_id.strip())

    @classmethod
    def create(
        cls,
        *,
        post_id: str,
        liker_id: str = LOCAL_LIKER_ID,
        created_at: datetime | None = None,
        id: str | None = None,
    ) -> "FeedReaction":
        return cls(
            id=id or uuid4().hex,
            post_id=post_id,
            liker_id=liker_id,
            created_at=created_at or _utcnow(),
        )
