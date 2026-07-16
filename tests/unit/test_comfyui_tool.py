"""BDD for ``ComfyImageTool``.

Mocks the HTTP client so we don't need a live ComfyUI. Covers:

- happy path: positive prompt → queued → completed → image stored →
  attachment URL points into ``/uploads/characters/{id}/tools/``
- character appearance + emotion auto-prepended to the positive prompt
- aspect ratio mapping
- queue failure → ``ToolResult.failure`` (not exception)
- timeout → ``ToolResult.failure``
- missing positive → validation error
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from kokoro_link.contracts.object_storage import StoredObject
from kokoro_link.contracts.tool import ToolContext
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.tools.comfyui.client import (
    ComfyUiError,
    ComfyUiTimeout,
)
from kokoro_link.infrastructure.tools.comfyui.tool import ComfyImageTool
from kokoro_link.infrastructure.tools.comfyui.workflow import (
    DEFAULT_WORKFLOW_FILE,
    WorkflowBuilder,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.repositories.in_memory_generation_usage import (
    InMemoryGenerationUsageRepository,
)
from kokoro_link.infrastructure.usage.recorder import BackgroundUsageEventRecorder


class _FakeClient:
    """Stand-in for ``AsyncComfyUiClient`` that records what was asked
    and fabricates a history entry pointing at on-disk fake images."""

    def __init__(
        self,
        *,
        raise_on_queue: Exception | None = None,
        raise_on_wait: Exception | None = None,
        num_images: int = 1,
    ) -> None:
        self.queued_prompts: list[dict] = []
        self.raise_on_queue = raise_on_queue
        self.raise_on_wait = raise_on_wait
        self.num_images = num_images
        self._last_prompt_id = "pid-1"
        self._fake_image_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16

    async def queue_prompt(self, prompt: dict) -> str:
        if self.raise_on_queue is not None:
            raise self.raise_on_queue
        self.queued_prompts.append(prompt)
        return self._last_prompt_id

    async def wait_for_completion(self, prompt_id: str) -> dict:
        if self.raise_on_wait is not None:
            raise self.raise_on_wait
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

    async def download_image(
        self, *, filename: str, subfolder: str, folder_type: str,
    ) -> bytes:
        return self._fake_image_bytes


class _FailingObjectStorage(InMemoryObjectStorage):
    async def put_bytes(
        self,
        *,
        object_key: str,
        content: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        raise RuntimeError("storage unavailable")


def _character() -> Character:
    return Character.create(
        name="Yuki",
        summary="",
        personality=[], interests=[], speaking_style="soft",
        boundaries=[],
        state=CharacterState(
            emotion="smiling", affection=50, fatigue=0, trust=50, energy=100,
        ),
        appearance="long black hair, red ribbon, school uniform",
        allowed_tools=["generate_image"],
    )


def _tool(
    client: _FakeClient,
    *,
    uploads_dir: Path,
    object_storage: InMemoryObjectStorage | None = None,
    usage_recorder=None,
) -> ComfyImageTool:
    from kokoro_link.infrastructure.tools.comfyui.generator import (
        ComfyPortraitGenerator,
    )
    from tests.unit._image_provider_stub import StaticActiveImageProvider

    return ComfyImageTool(
        image_provider=StaticActiveImageProvider(
            ComfyPortraitGenerator(
                client=client,  # type: ignore[arg-type]
                workflow_builder=WorkflowBuilder(DEFAULT_WORKFLOW_FILE),
            ),
        ),
        uploads_dir=uploads_dir,
        object_storage=object_storage or InMemoryObjectStorage(public_base_url="/uploads"),
        usage_recorder=usage_recorder,
    )


@pytest.mark.asyncio
async def test_happy_path_returns_image_attachment(tmp_path: Path) -> None:
    client = _FakeClient()
    tool = _tool(client, uploads_dir=tmp_path)

    result = await tool.invoke(ToolContext(
        character=_character(),
        arguments={"positive": "1girl, window, soft lighting"},
    ))

    assert result.ok is True
    assert len(result.attachments) == 1
    att = result.attachments[0]
    assert att.kind == "image"
    assert att.mime_type == "image/png"
    character_id = _character().id
    # Character-id isn't deterministic because of uuid4; just check the
    # prefix + subdirectory shape.
    assert att.url.startswith("/uploads/characters/")
    assert "/tools/" in att.url
    assert att.url.endswith(".png")

    storage = tool._object_storage
    assert storage is not None
    object_key = storage.object_key_from_url(att.url)
    assert object_key is not None
    assert await storage.get_bytes(object_key=object_key) == client._fake_image_bytes


@pytest.mark.asyncio
async def test_chat_tool_requests_single_image_and_returns_single_attachment(
    tmp_path: Path,
) -> None:
    client = _FakeClient(num_images=3)
    tool = _tool(client, uploads_dir=tmp_path)

    result = await tool.invoke(ToolContext(
        character=_character(),
        arguments={"positive": "three variants"},
    ))

    assert result.ok is True
    assert client.queued_prompts[0]["5"]["inputs"]["batch_size"] == 1
    assert len(result.attachments) == 1
    assert result.output_text == "已產生 1 張圖片"


@pytest.mark.asyncio
async def test_chat_tool_records_image_usage(tmp_path: Path) -> None:
    client = _FakeClient(num_images=3)
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    tool = _tool(client, uploads_dir=tmp_path, usage_recorder=usage_recorder)

    result = await tool.invoke(ToolContext(
        character=_character(),
        arguments={"positive": "three variants", "aspect": "square"},
        recent_dialogue="user: 想看你剛剛說的樣子",
        user_attachment_urls=("data:image/png;base64,abc",),
    ))
    await usage_recorder.flush()

    rows = await usage_events.list_recent()
    assert result.ok is True
    assert len(rows) == 1
    row = rows[0]
    assert row.capability == "image"
    assert row.feature_key == "chat_image_tool"
    assert row.source_surface == "chat_image_tool"
    assert row.profile_id == "stub"
    assert row.quantity.usage_unit == "image"
    assert row.quantity.input_quantity == 1
    assert row.quantity.output_quantity == 3
    assert row.quantity.billable_quantity == 3
    assert row.artifact_count == 1
    assert row.metadata["aspect"] == "square"
    assert row.metadata["recent_dialogue"] is True
    assert row.metadata["user_attachment_count"] == 1


@pytest.mark.asyncio
async def test_chat_tool_records_usage_when_storage_fails_after_provider_call(
    tmp_path: Path,
) -> None:
    client = _FakeClient(num_images=1)
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    tool = _tool(
        client,
        uploads_dir=tmp_path,
        object_storage=_FailingObjectStorage(public_base_url="/uploads"),
        usage_recorder=usage_recorder,
    )

    result = await tool.invoke(ToolContext(
        character=_character(),
        arguments={"positive": "portrait", "aspect": "square"},
    ))
    await usage_recorder.flush()

    rows = await usage_events.list_recent()
    assert result.ok is False
    assert len(rows) == 1
    row = rows[0]
    assert row.capability == "image"
    assert row.feature_key == "chat_image_tool"
    assert row.status == "failed"
    assert row.error_code == "RuntimeError"
    assert row.quantity.output_quantity == 1
    assert row.quantity.billable_quantity == 1
    assert row.artifact_count == 0


@pytest.mark.asyncio
async def test_appearance_and_emotion_prepended_to_prompt(tmp_path: Path) -> None:
    client = _FakeClient()
    tool = _tool(client, uploads_dir=tmp_path)

    await tool.invoke(ToolContext(
        character=_character(),
        arguments={"positive": "窗邊微笑"},
    ))

    assert len(client.queued_prompts) == 1
    rendered = client.queued_prompts[0]
    positive = rendered["6"]["inputs"]["text"]
    assert "masterpiece" in positive  # quality boilerplate
    assert "long black hair, red ribbon" in positive  # appearance
    assert "smiling" in positive  # state.emotion
    assert "窗邊微笑" in positive  # user-requested positive


@pytest.mark.asyncio
async def test_landscape_aspect_sets_width_height(tmp_path: Path) -> None:
    client = _FakeClient()
    tool = _tool(client, uploads_dir=tmp_path)

    await tool.invoke(ToolContext(
        character=_character(),
        arguments={"positive": "scenery, mountains", "aspect": "landscape"},
    ))

    rendered = client.queued_prompts[0]
    assert rendered["5"]["inputs"]["width"] == 1216
    assert rendered["5"]["inputs"]["height"] == 832


@pytest.mark.asyncio
async def test_queue_failure_returns_failure(tmp_path: Path) -> None:
    client = _FakeClient(raise_on_queue=ComfyUiError("bad prompt"))
    tool = _tool(client, uploads_dir=tmp_path)

    result = await tool.invoke(ToolContext(
        character=_character(),
        arguments={"positive": "x"},
    ))

    assert result.ok is False
    assert "ComfyUI" in (result.error or "")


@pytest.mark.asyncio
async def test_timeout_returns_failure(tmp_path: Path) -> None:
    client = _FakeClient(raise_on_wait=ComfyUiTimeout("took too long"))
    tool = _tool(client, uploads_dir=tmp_path)

    result = await tool.invoke(ToolContext(
        character=_character(),
        arguments={"positive": "x"},
    ))

    assert result.ok is False
    assert "逾時" in (result.error or "")


@pytest.mark.asyncio
async def test_missing_positive_is_validation_error(tmp_path: Path) -> None:
    client = _FakeClient()
    tool = _tool(client, uploads_dir=tmp_path)

    result = await tool.invoke(ToolContext(
        character=_character(),
        arguments={"aspect": "portrait"},
    ))

    assert result.ok is False
    assert "positive" in (result.error or "")


@pytest.mark.asyncio
async def test_caption_passed_through_to_attachment(tmp_path: Path) -> None:
    client = _FakeClient()
    tool = _tool(client, uploads_dir=tmp_path)

    result = await tool.invoke(ToolContext(
        character=_character(),
        arguments={
            "positive": "a",
            "caption": "這是我現在的樣子！",
        },
    ))

    assert result.ok is True
    assert result.output_text == "這是我現在的樣子！"
    assert result.attachments[0].caption == "這是我現在的樣子！"
