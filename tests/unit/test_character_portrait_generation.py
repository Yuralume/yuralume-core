"""BDD for ``CharacterImageService.generate_portrait``.

Tests the new path: operator clicks "用 AI 生成一張" in settings →
service calls the shared ``ComfyPortraitGenerator`` → bytes become a
regular Object Storage portrait (``image_urls``), NOT a tool attachment.

We mock the ComfyUI client to avoid network / GPU dependency.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_image_service import (
    CharacterImageService,
    GenerationDisabledError,
    GenerationFailedError,
    MAX_IMAGES_PER_CHARACTER,
    TooManyImagesError,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.domain.value_objects.account_runtime_profile import (
    DEMO_ACCOUNT_RUNTIME_PROFILE,
)
from kokoro_link.application.services.visual_generation_style import (
    VisualGenerationStyleService,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.tools.comfyui.generator import (
    ComfyPortraitGenerator,
)
from kokoro_link.infrastructure.tools.comfyui.workflow import (
    DEFAULT_WORKFLOW_FILE,
    WorkflowBuilder,
)
from tests.unit._image_provider_stub import StaticActiveImageProvider


_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


class _FakeClient:
    def __init__(
        self,
        *,
        num_images: int = 1,
        raise_on_queue: Exception | None = None,
    ) -> None:
        self.queued_prompts: list[dict] = []
        self.num_images = num_images
        self._raise = raise_on_queue

    async def queue_prompt(self, prompt: dict) -> str:
        if self._raise is not None:
            raise self._raise
        self.queued_prompts.append(prompt)
        return "pid-1"

    async def wait_for_completion(self, prompt_id: str) -> dict:
        return {
            "outputs": {
                "9": {
                    "images": [
                        {
                            "filename": f"out_{i}.png",
                            "subfolder": "",
                            "type": "output",
                        }
                        for i in range(self.num_images)
                    ],
                },
            },
        }

    async def download_image(
        self, *, filename: str, subfolder: str, folder_type: str,
    ) -> bytes:
        return _PNG_BYTES


class _RecordingImageProvider:
    def __init__(self) -> None:
        self.positives: list[str] = []

    async def generate(self, **kwargs) -> list[bytes]:  # noqa: ANN003
        self.positives.append(str(kwargs.get("positive") or ""))
        return [_PNG_BYTES]


def _service_with_generator(
    tmp_path: Path, *, num_images: int = 1,
    raise_on_queue: Exception | None = None,
) -> tuple[CharacterImageService, CharacterService, _FakeClient]:
    repo = InMemoryCharacterRepository()
    client = _FakeClient(
        num_images=num_images, raise_on_queue=raise_on_queue,
    )
    generator = ComfyPortraitGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
    )
    service = CharacterImageService(
        character_repository=repo,
        uploads_dir=tmp_path,
        image_provider=StaticActiveImageProvider(generator),
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
    )
    character_service = CharacterService(repo)
    return service, character_service, client


@pytest.mark.asyncio
async def test_generate_portrait_appends_to_image_urls(tmp_path: Path) -> None:
    service, chars, client = _service_with_generator(tmp_path)
    created = await chars.create_character(CreateCharacterRequest(
        name="Yui",
        appearance="long black hair, red ribbon",
        gender_identity="非二元",
        third_person_pronoun="TA",
        visual_gender_presentation="androgynous teen",
    ))

    updated = await service.generate_portrait(
        created.id, positive="cafe, warm light",
    )

    assert len(updated.image_urls) == 1
    url = updated.image_urls[0]
    assert url.startswith(f"/uploads/characters/{created.id}/")
    assert "/tools/" not in url  # goes to the main portrait dir, not tools
    storage = service._object_storage
    assert storage is not None
    object_key = storage.object_key_from_url(url)
    assert object_key is not None
    assert await storage.get_bytes(object_key=object_key) == _PNG_BYTES
    # Appearance + positive both fed into the prompt.
    queued = client.queued_prompts[0]
    positive_text = queued["6"]["inputs"]["text"]
    assert "long black hair" in positive_text
    assert "非二元" in positive_text
    assert "androgynous teen" in positive_text
    assert "cafe, warm light" in positive_text


@pytest.mark.asyncio
async def test_generate_portrait_applies_visual_style_preference(
    tmp_path: Path,
) -> None:
    repo = InMemoryCharacterRepository()
    provider = _RecordingImageProvider()
    from tests.unit._image_provider_stub import StaticActiveImageProvider

    style_service = VisualGenerationStyleService(
        preferences=InMemoryPreferencesRepository(),
    )
    service = CharacterImageService(
        character_repository=repo,
        uploads_dir=tmp_path,
        image_provider=StaticActiveImageProvider(provider),
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        visual_style_service=style_service,
    )
    chars = CharacterService(repo)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))
    entity = await repo.get(created.id)
    assert entity is not None
    await style_service.set_style("realistic", user_id=entity.user_id)

    await service.generate_portrait(created.id, positive="cafe, warm light")

    assert provider.positives
    assert "cafe, warm light" in provider.positives[0]
    assert "realistic live-action" in provider.positives[0]
    assert "Avoid anime" in provider.positives[0]


@pytest.mark.asyncio
async def test_generate_portrait_prefers_character_visual_style(
    tmp_path: Path,
) -> None:
    repo = InMemoryCharacterRepository()
    provider = _RecordingImageProvider()

    style_service = VisualGenerationStyleService(
        preferences=InMemoryPreferencesRepository(),
    )
    service = CharacterImageService(
        character_repository=repo,
        uploads_dir=tmp_path,
        image_provider=StaticActiveImageProvider(provider),
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        visual_style_service=style_service,
    )
    chars = CharacterService(repo)
    created = await chars.create_character(
        CreateCharacterRequest(
            name="Yui",
            visual_generation_style="anime",
        ),
    )
    entity = await repo.get(created.id)
    assert entity is not None
    await style_service.set_style("realistic", user_id=entity.user_id)

    await service.generate_portrait(created.id, positive="cafe, warm light")

    assert provider.positives
    assert "polished anime illustration" in provider.positives[0]
    assert "realistic live-action" not in provider.positives[0]


@pytest.mark.asyncio
async def test_generate_portrait_requires_generator(tmp_path: Path) -> None:
    repo = InMemoryCharacterRepository()
    service = CharacterImageService(
        character_repository=repo, uploads_dir=tmp_path,
        image_provider=None,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
    )
    character_service = CharacterService(repo)
    created = await character_service.create_character(
        CreateCharacterRequest(name="Yui"),
    )

    with pytest.raises(GenerationDisabledError):
        await service.generate_portrait(created.id, positive="x")


@pytest.mark.asyncio
async def test_generate_portrait_surfaces_comfy_failure(tmp_path: Path) -> None:
    from kokoro_link.infrastructure.tools.comfyui.client import ComfyUiError

    service, chars, _ = _service_with_generator(
        tmp_path, raise_on_queue=ComfyUiError("server down"),
    )
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))

    with pytest.raises(GenerationFailedError):
        await service.generate_portrait(created.id, positive="x")


class _StaticProfileResolver:
    def __init__(self, profile) -> None:  # noqa: ANN001
        self._profile = profile

    async def resolve_for_operator(self, operator_id: str):  # noqa: ANN201
        return self._profile


@pytest.mark.asyncio
async def test_primary_init_bypasses_album_gate_while_manual_blocked(
    tmp_path: Path,
) -> None:
    repo = InMemoryCharacterRepository()
    client = _FakeClient()
    generator = ComfyPortraitGenerator(
        client=client,  # type: ignore[arg-type]
        workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
    )
    service = CharacterImageService(
        character_repository=repo,
        uploads_dir=tmp_path,
        image_provider=StaticActiveImageProvider(generator),
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
        account_runtime_profile_resolver=_StaticProfileResolver(
            DEMO_ACCOUNT_RUNTIME_PROFILE,
        ),
    )
    chars = CharacterService(repo)
    created = await chars.create_character(CreateCharacterRequest(name="Demo"))

    # Demo profile disables album generation -> the repeatable manual portrait
    # path stays blocked.
    with pytest.raises(GenerationDisabledError):
        await service.generate_portrait(created.id, positive="x")

    # The one-time primary portrait is bounded by max_characters, so it bypasses
    # the album gate and still generates (important onboarding a-ha).
    updated = await service.generate_portrait(
        created.id, positive="x", is_primary_init=True,
    )
    assert len(updated.image_urls) == 1


@pytest.mark.asyncio
async def test_generate_portrait_respects_image_cap(tmp_path: Path) -> None:
    service, chars, _ = _service_with_generator(tmp_path)
    created = await chars.create_character(CreateCharacterRequest(name="Yui"))
    # Fill up to the cap with placeholder uploads.
    for _ in range(MAX_IMAGES_PER_CHARACTER):
        await service.add_image(
            created.id, data=_PNG_BYTES, mime_type="image/png",
            original_filename="x.png",
        )

    with pytest.raises(TooManyImagesError):
        await service.generate_portrait(created.id, positive="x")
