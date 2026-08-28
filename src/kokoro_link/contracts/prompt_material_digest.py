"""Ports and DTOs for chat prompt material digest."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from kokoro_link.domain.entities.character import Character


@dataclass(frozen=True, slots=True)
class PromptMaterialDigestContext:
    character_id: str
    operator_id: str
    emotion_events: tuple[str, ...] = ()
    self_reflections: tuple[str, ...] = ()
    story_events: tuple[str, ...] = ()
    story_arc: tuple[str, ...] = ()
    recent_feed_posts: tuple[str, ...] = ()
    source_language: str = ""
    content_tolerance: str = "frontier"


@dataclass(frozen=True, slots=True)
class PromptMaterialDigest:
    bullets: tuple[str, ...]
    digest_metadata: dict[str, Any] = field(default_factory=dict)


class PromptMaterialDigestPort(Protocol):
    async def digest(
        self,
        context: PromptMaterialDigestContext,
        *,
        character: Character | None = None,
    ) -> PromptMaterialDigest | None:
        """Return fact bullets for poetic prompt material, or ``None`` fail-soft."""


@dataclass(frozen=True, slots=True)
class StoredPromptMaterialDigest:
    """One budgeted digest, as it sits between two turns.

    ``content_tolerance`` rides *with* the digest rather than being
    re-derived on read: the bullets were generated to a tolerance, so the
    reader has to be able to ask "is this the tolerance I am rendering
    for?" and refuse otherwise. ``updated_at`` is what bounds staleness —
    a row has no natural expiry and a player who comes back after a month
    must not be given a month-old summary as if it were current.
    """

    character_id: str
    operator_id: str
    content_tolerance: str
    digest: PromptMaterialDigest
    updated_at: datetime


class PromptMaterialDigestStorePort(Protocol):
    """Where a turn's post-turn leaves the digest for the *next* turn.

    Deliberately a store, not a cache: on hosted the post-turn runs on a
    worker process while chat is served from the API process, so anything
    process-local is written by one and never read by the other. One row
    per ``(character, operator)`` — the digest describes a relationship's
    current material, and two live rows for one pair would just be a way
    to serve whichever the reader happened to find.
    """

    async def get(
        self, *, character_id: str, operator_id: str,
    ) -> StoredPromptMaterialDigest | None:
        """The pair's budgeted digest, or ``None`` before the first one."""

    async def upsert(self, stored: StoredPromptMaterialDigest) -> bool:
        """Land the row unless a **newer** one is already there.

        ``updated_at`` doubles as the version: it is the instant the
        writer read the source material, so of two concurrent post-turns
        the larger stamp is by definition the fresher read. A write whose
        stamp is older than the stored row is a slow writer arriving late
        and is refused — it must not resurrect material the fresher read
        has already superseded.

        Returns whether the write landed. ``False`` is a normal outcome
        ("somebody newer got here first"), never an error.
        """

    async def delete(
        self,
        *,
        character_id: str,
        operator_id: str | None = None,
        not_newer_than: datetime | None = None,
    ) -> int:
        """Drop rows and report how many.

        ``operator_id=None`` drops every operator's row for the character
        — what a turn undo needs, since the undo journal names a
        character and no operator.

        ``not_newer_than`` bounds the delete by the same version stamp
        ``upsert`` compares on: a writer withdrawing *its own* row must
        not take out a fresher one that landed in between. Undo passes
        nothing — a reversed turn's material has to go regardless of who
        wrote last.
        """
