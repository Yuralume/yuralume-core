"""BDD for ``ComfyImageTool`` ↔ ``AlbumService`` integration.

Verifies that every successful generation results in an album row, and
that album-side failures don't break the tool-facing contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.services.album_service import AlbumService
from kokoro_link.contracts.tool import ToolContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_album import (
    InMemoryAlbumRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.tools.comfyui.generator import (
    ComfyPortraitGenerator,
)
from tests.unit._image_provider_stub import StaticActiveImageProvider
from kokoro_link.infrastructure.tools.comfyui.tool import ComfyImageTool
from kokoro_link.infrastructure.tools.comfyui.workflow import (
    DEFAULT_WORKFLOW_FILE,
    WorkflowBuilder,
)


class _FakeClient:
    def __init__(self, num_images: int = 1) -> None:
        self.num_images = num_images
        self._fake_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    async def queue_prompt(self, prompt: dict) -> str:
        return "pid"

    async def wait_for_completion(self, prompt_id: str) -> dict:
        return {
            "outputs": {
                "9": {
                    "images": [
                        {"filename": f"out_{i}.png", "subfolder": "", "type": "output"}
                        for i in range(self.num_images)
                    ],
                },
            },
        }

    async def download_image(self, **_: object) -> bytes:
        return self._fake_bytes


def _character() -> Character:
    return Character.create(
        name="Yuki",
        summary="",
        personality=[], interests=[], speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
        allowed_tools=["generate_image"],
    )


async def _build_tool_and_album(tmp_path: Path, *, num_images: int = 1):
    character = _character()
    char_repo = InMemoryCharacterRepository()
    await char_repo.save(character)
    storage = InMemoryObjectStorage(public_base_url="/uploads")

    album_service = AlbumService(
        album_repository=InMemoryAlbumRepository(),
        character_repository=char_repo,
        uploads_dir=tmp_path,
        object_storage=storage,
    )

    tool = ComfyImageTool(
        image_provider=StaticActiveImageProvider(
            ComfyPortraitGenerator(
                client=_FakeClient(num_images=num_images),  # type: ignore[arg-type]
                workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
            ),
        ),
        uploads_dir=tmp_path,
        album_service=album_service,
        object_storage=storage,
    )
    return tool, album_service, character


@pytest.mark.asyncio
async def test_successful_generation_writes_album_row(tmp_path: Path) -> None:
    tool, album_service, character = await _build_tool_and_album(tmp_path)

    result = await tool.invoke(ToolContext(
        character=character,
        arguments={
            "positive": "1girl, cafe, afternoon light",
            "caption": "在咖啡廳的午後",
        },
    ))

    assert result.ok is True
    assert len(result.attachments) == 1
    expected_url = result.attachments[0].url

    # Album got a row keyed to the generated file
    items = await album_service.list_for_character(character.id)
    assert len(items) == 1
    album_item = items[0]
    assert album_item.url == expected_url
    assert album_item.source == "tool"
    assert album_item.caption == "在咖啡廳的午後"
    # Byte size roughly matches what we wrote
    assert album_item.byte_size is not None
    assert album_item.byte_size > 0


@pytest.mark.asyncio
async def test_chat_tool_stores_only_delivered_image_when_provider_overreturns(
    tmp_path: Path,
) -> None:
    tool, album_service, character = await _build_tool_and_album(
        tmp_path, num_images=3,
    )

    result = await tool.invoke(ToolContext(
        character=character,
        arguments={"positive": "three variants"},
    ))

    assert result.ok is True
    assert len(result.attachments) == 1
    items = await album_service.list_for_character(character.id)
    assert len(items) == 1
    assert items[0].url == result.attachments[0].url


@pytest.mark.asyncio
async def test_album_service_failure_does_not_poison_tool_result(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """If the album service raises, the user still gets their image.
    Album capture is best-effort; tool reliability is not."""
    character = _character()
    char_repo = InMemoryCharacterRepository()
    await char_repo.save(character)

    class _BrokenAlbum:
        async def add_auto(self, **_: object) -> None:
            raise RuntimeError("DB blip")

    tool = ComfyImageTool(
        image_provider=StaticActiveImageProvider(
            ComfyPortraitGenerator(
                client=_FakeClient(),  # type: ignore[arg-type]
                workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
            ),
        ),
        uploads_dir=tmp_path,
        album_service=_BrokenAlbum(),  # type: ignore[arg-type]
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
    )

    with caplog.at_level("ERROR"):
        result = await tool.invoke(ToolContext(
            character=character,
            arguments={"positive": "test"},
        ))

    assert result.ok is True
    assert len(result.attachments) == 1
    assert any(
        "album.add_auto failed" in r.message for r in caplog.records
    )


@pytest.mark.asyncio
async def test_tool_without_album_service_still_works(tmp_path: Path) -> None:
    """Backwards compat: older container wirings or tests may build the
    tool without an album. That path must continue returning
    attachments exactly as before."""
    tool = ComfyImageTool(
        image_provider=StaticActiveImageProvider(
            ComfyPortraitGenerator(
                client=_FakeClient(),  # type: ignore[arg-type]
                workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
            ),
        ),
        uploads_dir=tmp_path,
        album_service=None,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
    )

    result = await tool.invoke(ToolContext(
        character=_character(),
        arguments={"positive": "test"},
    ))
    assert result.ok is True
    assert len(result.attachments) == 1
