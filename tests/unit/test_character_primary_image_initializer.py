import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_image_service import (
    GenerationDisabledError,
)
from kokoro_link.application.services.character_primary_image_initializer import (
    CharacterPrimaryImageInitializer,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)


class _RecordingImageService:
    def __init__(
        self,
        repository: InMemoryCharacterRepository,
        *,
        crash: Exception | None = None,
    ) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.primary_init_flags: list[bool] = []
        self._repository = repository
        self._crash = crash

    async def generate_portrait(
        self,
        character_id: str,
        *,
        positive: str,
        aspect: str = "portrait",
        is_primary_init: bool = False,
    ):
        self.calls.append((character_id, positive, aspect))
        self.primary_init_flags.append(is_primary_init)
        if self._crash is not None:
            raise self._crash
        character = await self._repository.get(character_id)
        assert character is not None
        updated = character.with_image_urls(
            tuple([*character.image_urls, f"/uploads/characters/{character_id}/auto.png"]),
        )
        await self._repository.save(updated)
        return updated


@pytest.mark.asyncio
async def test_ensure_after_create_generates_missing_primary_image() -> None:
    repository = InMemoryCharacterRepository()
    character_service = CharacterService(repository)
    created = await character_service.create_character(
        CreateCharacterRequest(
            name="Airi",
            summary="夜間咖啡店的見習占星師",
            personality=["calm", "curious"],
            interests=["stargazing", "latte art"],
            appearance="silver bob hair, amber eyes",
            visual_gender_presentation="androgynous mage",
            world_frame="urban fantasy",
        ),
    )
    image_service = _RecordingImageService(repository)
    initializer = CharacterPrimaryImageInitializer(
        character_service=character_service,
        character_image_service=image_service,  # type: ignore[arg-type]
    )

    result = await initializer.ensure_after_create(created.id)

    assert result.character_id == created.id
    assert result.image_generated is True
    assert result.character is not None
    assert result.character.image_urls == [
        f"/uploads/characters/{created.id}/auto.png",
    ]
    assert image_service.calls[0][0] == created.id
    assert image_service.calls[0][2] == "portrait"
    # Primary init must mark the call so it bypasses the album-generation gate
    # (bounded by character-creation limits, not the spammable manual album path).
    assert image_service.primary_init_flags == [True]
    positive = image_service.calls[0][1]
    assert "primary character portrait" in positive
    assert "夜間咖啡店的見習占星師" in positive
    assert "silver bob hair, amber eyes" in positive
    assert "androgynous mage" in positive
    assert "calm, curious" in positive
    assert "stargazing, latte art" in positive
    assert "urban fantasy" in positive


@pytest.mark.asyncio
async def test_ensure_after_create_uses_animal_primary_prompt() -> None:
    repository = InMemoryCharacterRepository()
    character_service = CharacterService(repository)
    created = await character_service.create_character(
        CreateCharacterRequest(
            name="Mochi",
            summary="住在陽台上的橘貓",
            appearance="一隻短毛橘貓，四足姿態，圓眼睛，戴著小鈴鐺",
            visual_gender_presentation="可愛寵物貓",
            visual_subject_type="animal",
        ),
    )
    image_service = _RecordingImageService(repository)
    initializer = CharacterPrimaryImageInitializer(
        character_service=character_service,
        character_image_service=image_service,  # type: ignore[arg-type]
    )

    result = await initializer.ensure_after_create(created.id)

    assert result.image_generated is True
    positive = image_service.calls[0][1]
    assert "primary non-human animal reference image" in positive
    assert "full animal body visible" in positive
    assert "Visual subject type: non-human animal." in positive
    assert "Species/body plan: domestic cat." in positive
    assert "Do NOT anthropomorphize" in positive
    assert "Visual gender presentation" not in positive
    assert "waist-up composition" not in positive


@pytest.mark.asyncio
async def test_ensure_after_create_skips_existing_image() -> None:
    repository = InMemoryCharacterRepository()
    character_service = CharacterService(repository)
    created = await character_service.create_character(
        CreateCharacterRequest(
            name="Airi",
            image_urls=["/uploads/characters/existing.png"],
        ),
    )
    image_service = _RecordingImageService(repository)
    initializer = CharacterPrimaryImageInitializer(
        character_service=character_service,
        character_image_service=image_service,  # type: ignore[arg-type]
    )

    result = await initializer.ensure_after_create(created.id)

    assert result.image_generated is False
    assert result.character is not None
    assert result.character.image_urls == ["/uploads/characters/existing.png"]
    assert image_service.calls == []


@pytest.mark.asyncio
async def test_ensure_after_create_is_fail_soft_when_generation_unavailable() -> None:
    repository = InMemoryCharacterRepository()
    character_service = CharacterService(repository)
    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi"),
    )
    initializer = CharacterPrimaryImageInitializer(
        character_service=character_service,
        character_image_service=_RecordingImageService(
            repository,
            crash=GenerationDisabledError("no image profile"),
        ),  # type: ignore[arg-type]
    )

    result = await initializer.ensure_after_create(created.id)

    assert result.character_id == created.id
    assert result.image_generated is False
    assert result.character is not None
    assert result.character.image_urls == []


@pytest.mark.asyncio
async def test_ensure_after_create_ignores_missing_character() -> None:
    repository = InMemoryCharacterRepository()
    character_service = CharacterService(repository)
    initializer = CharacterPrimaryImageInitializer(
        character_service=character_service,
        character_image_service=_RecordingImageService(repository),  # type: ignore[arg-type]
    )

    result = await initializer.ensure_after_create("missing")

    assert result.character_id == "missing"
    assert result.character is None
    assert result.image_generated is False
