"""Fusion material-richness stats surface (Creator Studio C1-P1).

Backs the character-picker richness badge: for a set of the current
user's characters, returns how much fusion-usable material each one has
(memory count + total chars) and its badge tier (``rich`` / ``ok`` /
``sparse``).

Ownership-scoped — stats for characters the caller does not own are
silently omitted (same non-enumeration boundary as the fusion-story
create route) so this endpoint can't be used to probe another user's
character ids or their memory volume.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from kokoro_link.api.dependencies import (
    get_container,
    get_current_user_id,
)
from kokoro_link.bootstrap.container import ServiceContainer


router = APIRouter(tags=["studio-material"])

_MAX_CHARACTER_IDS = 20


class CharacterMaterialStatsResponse(BaseModel):
    character_id: str
    memory_count: int
    total_chars: int
    tier: str


class FusionMaterialStatsResponse(BaseModel):
    stats: list[CharacterMaterialStatsResponse]


def _parse_ids(raw: str) -> list[str]:
    """Comma-separated ids → de-duplicated, order-preserving list."""
    seen: set[str] = set()
    ordered: list[str] = []
    for part in raw.split(","):
        cid = part.strip()
        if not cid or cid in seen:
            continue
        seen.add(cid)
        ordered.append(cid)
    return ordered


async def _owned_ids(
    container: ServiceContainer,
    character_ids: list[str],
    user_id: str,
) -> list[str]:
    """Filter to ids the current user owns, preserving order.

    Non-owned / missing ids are silently dropped — mirrors
    ``fusion_story._assert_characters_owned`` but without raising, so a
    caller can't learn another user's character ids or memory volume."""
    service = getattr(container, "character_service", None)
    if service is None:
        return character_ids
    owned: list[str] = []
    for cid in character_ids:
        try:
            character = await service.get_character_entity(
                cid, user_id=user_id,
            )
        except TypeError:
            character = await service.get_character_entity(cid)
            if (
                character is not None
                and getattr(character, "user_id", user_id) != user_id
            ):
                character = None
        if character is not None:
            owned.append(cid)
    return owned


@router.get(
    "/studio/fusion-material-stats",
    response_model=FusionMaterialStatsResponse,
)
async def get_fusion_material_stats(
    character_ids: str = Query(default=""),
    container: ServiceContainer = Depends(get_container),
    current_user_id: str = Depends(get_current_user_id),
) -> FusionMaterialStatsResponse:
    ids = _parse_ids(character_ids)
    if not ids:
        return FusionMaterialStatsResponse(stats=[])
    if len(ids) > _MAX_CHARACTER_IDS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"at most {_MAX_CHARACTER_IDS} character_ids per request"
            ),
        )
    service = getattr(container, "fusion_material_stats_service", None)
    if service is None:
        return FusionMaterialStatsResponse(stats=[])
    owned = await _owned_ids(container, ids, current_user_id)
    if not owned:
        return FusionMaterialStatsResponse(stats=[])
    stats = await service.stats_for(owned)
    return FusionMaterialStatsResponse(
        stats=[
            CharacterMaterialStatsResponse(
                character_id=item.character_id,
                memory_count=item.memory_count,
                total_chars=item.total_chars,
                tier=item.tier,
            )
            for item in stats
        ],
    )
