"""Surface-aware claim arbiter for the per-character event inbox.

The dispenser is the single point that hands out a curated event to a
surface (proactive / feed / drama). Claims are atomic at the
repository level — exactly one surface wins per inbox row. Once
claimed, the row is locked to that surface forever; subsequent
``claim`` calls return ``None``.

This guarantees:

- The same news item is never published in a LumeGram post AND a
  proactive DM in the same window.
- Re-running the proactive scheduler twice in a row doesn't burn
  through the inbox (each surface has its own most-recent claim).
- Chat / read-only consumers can ``peek`` the inbox without
  consuming it.

Operational policy (cooldowns, daily caps, age) is enforced by the
caller — the dispenser is purely the lock primitive.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

from kokoro_link.contracts.character_event_inbox import (
    CharacterEventInboxRepositoryPort,
)
from kokoro_link.contracts.world_event import WorldEventRepositoryPort
from kokoro_link.domain.entities.character_event_inbox import (
    CharacterEventInboxItem,
)
from kokoro_link.domain.entities.world_event import WorldEvent

logger = logging.getLogger(__name__)


class ClaimedSeed(NamedTuple):
    item: CharacterEventInboxItem
    event: WorldEvent


class EventSeedDispenser:
    def __init__(
        self,
        *,
        inbox_repository: CharacterEventInboxRepositoryPort,
        world_event_repository: WorldEventRepositoryPort,
        max_age_days: int = 5,
    ) -> None:
        self._inbox = inbox_repository
        self._events = world_event_repository
        self._max_age_days = max_age_days

    async def claim(
        self, *, character_id: str, surface: str,
    ) -> ClaimedSeed | None:
        """Atomically claim the oldest unclaimed inbox row.

        Returns ``None`` when the inbox is empty, every candidate is
        beyond the age window, or the chosen row is already claimed
        (race lost). Caller treats ``None`` as "no seed this round" and
        falls through to whatever it was doing before.
        """
        if not (surface or "").strip():
            raise ValueError("surface must be non-empty")

        cutoff_published = (
            datetime.now(timezone.utc) - timedelta(days=self._max_age_days)
        )
        unclaimed = await self._inbox.list_for_character(
            character_id, unclaimed_only=True, limit=20,
        )
        for item in unclaimed:
            event = await self._events.get(item.world_event_id)
            if event is None:
                # Stale pointer — event deleted out from under us.
                # Drop the inbox row and continue.
                await self._inbox.delete_for_event(item.world_event_id)
                continue
            if event.published_at < cutoff_published:
                # Too stale to act on. Skip but keep the row — nightly
                # GC will sweep it.
                continue
            claimed = await self._inbox.claim(
                item.id, surface=surface, at=datetime.now(timezone.utc),
            )
            if claimed is None:
                # Lost the race; try the next candidate.
                continue
            return ClaimedSeed(item=claimed, event=event)
        return None

    async def commit(
        self, *, item_id: str, surface: str,
    ) -> ClaimedSeed | None:
        """Atomically claim a specific peeked row, by id.

        Pairs with :meth:`peek`: callers that want to defer the consume
        decision (e.g. the feed composer competes between multiple
        candidates and only one wins) peek to gather references, then
        commit the winner. Returns ``None`` when the row was already
        claimed by someone else (race lost) or no longer exists.
        """
        if not (surface or "").strip():
            raise ValueError("surface must be non-empty")
        claimed = await self._inbox.claim(
            item_id, surface=surface, at=datetime.now(timezone.utc),
        )
        if claimed is None:
            return None
        event = await self._events.get(claimed.world_event_id)
        if event is None:
            return None
        return ClaimedSeed(item=claimed, event=event)

    async def release(
        self, *, item_id: str, surface: str,
    ) -> bool:
        """Best-effort: undo a claim previously made by ``surface``.

        Used by surfaces that claim eagerly to preempt other surfaces
        but later decide not to publish (e.g. proactive decider returns
        ``should_send=False``). Only releases rows still owned by the
        same surface — never steals from another claimant.
        Returns ``True`` if a row was released, ``False`` otherwise.
        """
        if not (surface or "").strip():
            raise ValueError("surface must be non-empty")
        released = await self._inbox.release(
            item_id, surface=surface,
        )
        return released

    async def peek(
        self, *, character_id: str, limit: int = 3,
    ) -> list[ClaimedSeed]:
        """Read-only view of the most recent unclaimed seeds.

        Used by the chat path: the prompt builder hints the LLM about
        topical things the character has come across, but does not
        consume them. If the user actually engages the topic, a normal
        post-turn memory pass picks it up via the conversation."""
        result: list[ClaimedSeed] = []
        unclaimed = await self._inbox.list_for_character(
            character_id, unclaimed_only=True, limit=limit * 2,
        )
        cutoff_published = (
            datetime.now(timezone.utc) - timedelta(days=self._max_age_days)
        )
        for item in unclaimed:
            event = await self._events.get(item.world_event_id)
            if event is None or event.published_at < cutoff_published:
                continue
            result.append(ClaimedSeed(item=item, event=event))
            if len(result) >= limit:
                break
        return result
