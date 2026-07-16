"""Admin routes for site-wide character freeze control (CHARACTER_FREEZE_PLAN).

``GET  /admin/characters/overview``            → all characters, staleest first
``POST /admin/characters/{id}/freeze``          → immediate site-level freeze
``POST /admin/characters/{id}/unfreeze``        → clear the freeze flag

The admin console renders the overview as a table so operators can spot
characters that have gone idle (no chat, no admin action) and freeze them
to stop background scheduler work (proactive pings, feed composition,
schedule generation, etc.) without touching the persisted character state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status

from kokoro_link.api.dependencies import get_container, require_admin
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.domain.entities.character import (
    FREEZE_REASON_MANUAL,
    Character,
)

router = APIRouter(
    prefix="/admin/characters",
    tags=["admin-characters"],
    dependencies=[Depends(require_admin)],
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


_OLDEST = datetime.min.replace(tzinfo=timezone.utc)


def _idle_anchor(character: Character) -> datetime | None:
    """Effective idle anchor used to sort the overview staleest-first.

    Prefers the last real user interaction; falls back to row creation
    time for characters that have never been chatted with. ``None`` when
    neither is known — treated as the oldest possible so a character with
    no data at all sorts to the very front."""
    return character.state.last_active_at or character.created_at


def _overview_sort_key(character: Character) -> tuple[bool, datetime]:
    # Staleest first: no-anchor characters (never touched) to the very
    # front, then ascending by anchor. The leading bool groups the
    # None case so the aware ``_OLDEST`` sentinel is only ever compared
    # against other aware datetimes (no naive/aware TypeError).
    anchor = _idle_anchor(character)
    return (anchor is not None, anchor or _OLDEST)


def _overview_entry(character: Character) -> dict[str, Any]:
    return {
        "id": character.id,
        "name": character.name,
        "owner_user_id": character.user_id,
        "frozen": character.frozen,
        "frozen_at": _iso(character.frozen_at),
        "frozen_reason": character.frozen_reason,
        "last_active_at": _iso(character.state.last_active_at),
        "created_at": _iso(character.created_at),
        "proactive_enabled": character.proactive_enabled,
    }


@router.get("/overview")
async def get_characters_overview(
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    repo = container.character_repository
    if repo is None:
        return {"characters": [], "total": 0}

    characters = await repo.list()
    ranked = sorted(characters, key=_overview_sort_key)
    return {
        "characters": [_overview_entry(c) for c in ranked],
        "total": len(ranked),
    }


@router.post("/{character_id}/freeze")
async def freeze_character(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    repo = container.character_repository
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "character not found")
    now = datetime.now(timezone.utc)
    # The single targeted update is also the existence check — its bool
    # return closes the get-then-update race and saves a query. Admin
    # freezes are tagged ``manual`` so a user chat turn cannot silently
    # thaw an operator's deliberate action.
    updated = await repo.set_frozen(
        character_id, frozen=True, now=now, reason=FREEZE_REASON_MANUAL,
    )
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "character not found")
    return {
        "id": character_id,
        "frozen": True,
        "frozen_at": _iso(now),
        "frozen_reason": FREEZE_REASON_MANUAL,
    }


@router.post("/{character_id}/unfreeze")
async def unfreeze_character(
    character_id: str,
    container: ServiceContainer = Depends(get_container),
) -> dict[str, Any]:
    repo = container.character_repository
    if repo is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "character not found")
    now = datetime.now(timezone.utc)
    updated = await repo.set_frozen(character_id, frozen=False, now=now)
    if not updated:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "character not found")
    return {
        "id": character_id,
        "frozen": False,
        "frozen_at": None,
        "frozen_reason": None,
    }
