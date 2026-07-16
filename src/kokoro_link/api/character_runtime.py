from __future__ import annotations

from fastapi import BackgroundTasks

from kokoro_link.application.dto.character import CharacterResponse
from kokoro_link.bootstrap.container import ServiceContainer


async def ensure_character_primary_image(
    *,
    container: ServiceContainer,
    character: CharacterResponse,
    user_id: str,
) -> CharacterResponse:
    initializer = getattr(container, "character_primary_image_initializer", None)
    if initializer is None:
        return character
    result = await initializer.ensure_after_create(
        character.id,
        user_id=user_id,
    )
    return result.character or character


def enqueue_character_runtime_initialization(
    background_tasks: BackgroundTasks,
    *,
    container: ServiceContainer,
    character: CharacterResponse,
    user_id: str,
) -> None:
    initializer = getattr(container, "character_runtime_initializer", None)
    if initializer is None:
        return
    background_tasks.add_task(
        initializer.prepare_after_create,
        character.id,
        user_id=user_id,
    )
