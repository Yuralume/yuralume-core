import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.infrastructure.repositories.in_memory_characters import InMemoryCharacterRepository


@pytest.mark.asyncio
async def test_list_characters_returns_created_items() -> None:
    service = CharacterService(InMemoryCharacterRepository())

    await service.create_character(CreateCharacterRequest(name="Airi"))
    await service.create_character(CreateCharacterRequest(name="Mio"))

    characters = await service.list_characters()

    assert [character.name for character in characters] == ["Airi", "Mio"]
