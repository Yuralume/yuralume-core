"""BDD for ComfyUI LoRA injection + upload service.

Covers:

- ``WorkflowBuilder.build`` without LoRAs keeps the hand-authored
  wiring untouched (node 3 ``model`` ← node 4, nodes 6/7 ``clip`` ←
  node 4).
- With a single LoRA: one ``LoraLoader`` node inserted; node 3 + 6/7
  are rewired to it; strength applied to both ``strength_model``
  and ``strength_clip``.
- With multiple LoRAs: model+clip chained so the last node's outputs
  are what 3/6/7 consume.
- Upload writes a file to ``lora_dir`` and attaches to character.
- Remove, set_strength and attach_existing behave correctly.
- Rejecting disallowed extensions / path traversal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_lora_service import (
    CharacterLoraService,
    LoraNotFoundError,
    LoraUploadDisabledError,
    UnsupportedLoraTypeError,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.domain.entities.character import Character, CharacterLora
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.tools.comfyui.workflow import (
    DEFAULT_WORKFLOW_FILE,
    LoraSpec,
    PromptSpec,
    WorkflowBuilder,
)


# ---- workflow builder --------------------------------------------------

def _build_workflow(loras: tuple[LoraSpec, ...]) -> dict:
    builder = WorkflowBuilder(DEFAULT_WORKFLOW_FILE)
    return builder.build(PromptSpec(positive="test", loras=loras))


def test_builder_without_loras_leaves_wiring_untouched() -> None:
    prompt = _build_workflow(loras=())

    # 3.model still points at node 4 output 0.
    assert prompt["3"]["inputs"]["model"] == ["4", 0]
    assert prompt["6"]["inputs"]["clip"] == ["4", 1]
    assert prompt["7"]["inputs"]["clip"] == ["4", 1]
    # No LoraLoader nodes injected.
    assert "100" not in prompt


def test_builder_with_single_lora_inserts_node_and_rewires() -> None:
    prompt = _build_workflow(
        loras=(LoraSpec(name="kokkoro.safetensors", strength=0.8),),
    )

    assert "100" in prompt
    lora_node = prompt["100"]
    assert lora_node["class_type"] == "LoraLoader"
    assert lora_node["inputs"]["lora_name"] == "kokkoro.safetensors"
    assert lora_node["inputs"]["strength_model"] == 0.8
    assert lora_node["inputs"]["strength_clip"] == 0.8
    # Its own inputs point at checkpoint node 4.
    assert lora_node["inputs"]["model"] == ["4", 0]
    assert lora_node["inputs"]["clip"] == ["4", 1]
    # Sampler + CLIPs now read from the LoRA node.
    assert prompt["3"]["inputs"]["model"] == ["100", 0]
    assert prompt["6"]["inputs"]["clip"] == ["100", 1]
    assert prompt["7"]["inputs"]["clip"] == ["100", 1]


def test_builder_chains_multiple_loras() -> None:
    prompt = _build_workflow(
        loras=(
            LoraSpec(name="a.safetensors", strength=1.0),
            LoraSpec(name="b.safetensors", strength=0.5),
            LoraSpec(name="c.safetensors", strength=0.3),
        ),
    )

    # First LoRA reads from checkpoint.
    assert prompt["100"]["inputs"]["model"] == ["4", 0]
    assert prompt["100"]["inputs"]["clip"] == ["4", 1]
    # Each subsequent LoRA reads from the previous.
    assert prompt["101"]["inputs"]["model"] == ["100", 0]
    assert prompt["101"]["inputs"]["clip"] == ["100", 1]
    assert prompt["102"]["inputs"]["model"] == ["101", 0]
    assert prompt["102"]["inputs"]["clip"] == ["101", 1]
    # Final consumers point at the tail.
    assert prompt["3"]["inputs"]["model"] == ["102", 0]
    assert prompt["6"]["inputs"]["clip"] == ["102", 1]
    assert prompt["7"]["inputs"]["clip"] == ["102", 1]


# ---- lora service ------------------------------------------------------

@pytest.fixture
def service_and_character(tmp_path: Path) -> tuple[
    CharacterLoraService, CharacterService, str,
]:
    repo = InMemoryCharacterRepository()
    lora_dir = tmp_path / "loras"
    service = CharacterLoraService(
        character_repository=repo, lora_dir=lora_dir,
    )
    character_service = CharacterService(repo)
    return service, character_service, str(lora_dir)


@pytest.mark.asyncio
async def test_upload_writes_file_and_attaches(
    service_and_character,
    tmp_path: Path,
) -> None:
    service, chars, lora_dir = service_and_character
    created = await chars.create_character(CreateCharacterRequest(name="K"))

    updated = await service.upload(
        created.id,
        data=b"\x00" * 128,
        original_filename="MyLora.safetensors",
        strength=0.7,
    )

    disk_file = Path(lora_dir) / "MyLora.safetensors"
    assert disk_file.exists()
    assert disk_file.read_bytes() == b"\x00" * 128
    assert len(updated.loras) == 1
    assert updated.loras[0].name == "MyLora.safetensors"
    assert updated.loras[0].strength == 0.7


@pytest.mark.asyncio
async def test_upload_rejects_bad_extension(service_and_character) -> None:
    service, chars, _ = service_and_character
    created = await chars.create_character(CreateCharacterRequest(name="K"))

    with pytest.raises(UnsupportedLoraTypeError):
        await service.upload(
            created.id, data=b"x", original_filename="not_a_lora.png",
        )


@pytest.mark.asyncio
async def test_upload_strips_directory_traversal(service_and_character, tmp_path) -> None:
    service, chars, lora_dir = service_and_character
    created = await chars.create_character(CreateCharacterRequest(name="K"))

    updated = await service.upload(
        created.id,
        data=b"x",
        original_filename="../evil/inner.safetensors",
    )

    # Basename preserved; path component discarded.
    assert updated.loras[0].name == "inner.safetensors"
    assert (Path(lora_dir) / "inner.safetensors").exists()
    assert not (Path(lora_dir).parent / "evil").exists()


@pytest.mark.asyncio
async def test_remove_drops_from_list(service_and_character) -> None:
    service, chars, _ = service_and_character
    created = await chars.create_character(CreateCharacterRequest(name="K"))
    await service.upload(
        created.id, data=b"x", original_filename="a.safetensors",
    )

    updated = await service.remove(created.id, name="a.safetensors")

    assert updated.loras == ()


@pytest.mark.asyncio
async def test_remove_unknown_raises(service_and_character) -> None:
    service, chars, _ = service_and_character
    created = await chars.create_character(CreateCharacterRequest(name="K"))

    with pytest.raises(LoraNotFoundError):
        await service.remove(created.id, name="ghost.safetensors")


@pytest.mark.asyncio
async def test_set_strength_updates_only_target(service_and_character) -> None:
    service, chars, _ = service_and_character
    created = await chars.create_character(CreateCharacterRequest(name="K"))
    await service.upload(
        created.id, data=b"x", original_filename="a.safetensors", strength=1.0,
    )
    await service.upload(
        created.id, data=b"x", original_filename="b.safetensors", strength=1.0,
    )

    updated = await service.set_strength(
        created.id, name="b.safetensors", strength=0.4,
    )

    strengths = {l.name: l.strength for l in updated.loras}
    assert strengths == {"a.safetensors": 1.0, "b.safetensors": 0.4}


@pytest.mark.asyncio
async def test_attach_existing_without_disk_copy(service_and_character) -> None:
    service, chars, lora_dir = service_and_character
    created = await chars.create_character(CreateCharacterRequest(name="K"))

    # File doesn't exist on our disk — attach_existing only touches DB.
    updated = await service.attach_existing(
        created.id, name="external.safetensors", strength=0.9,
    )

    assert len(updated.loras) == 1
    assert updated.loras[0].name == "external.safetensors"
    assert updated.loras[0].strength == 0.9
    # Still no file was created.
    assert not (Path(lora_dir) / "external.safetensors").exists()


@pytest.mark.asyncio
async def test_list_available_returns_safetensors_only(
    service_and_character, tmp_path: Path,
) -> None:
    service, _, lora_dir = service_and_character
    (Path(lora_dir) / "keep.safetensors").write_bytes(b"x")
    (Path(lora_dir) / "ignore.txt").write_bytes(b"x")
    (Path(lora_dir) / "also.ckpt").write_bytes(b"x")

    available = service.list_available()

    assert available == ["also.ckpt", "keep.safetensors"]


@pytest.mark.asyncio
async def test_upload_disabled_when_no_dir_configured() -> None:
    repo = InMemoryCharacterRepository()
    service = CharacterLoraService(
        character_repository=repo, lora_dir="",
    )
    character_service = CharacterService(repo)
    created = await character_service.create_character(
        CreateCharacterRequest(name="K"),
    )

    with pytest.raises(LoraUploadDisabledError):
        await service.upload(
            created.id,
            data=b"x",
            original_filename="a.safetensors",
        )

    # attach_existing still works, even without disk — it's just a DB op.
    attached = await service.attach_existing(
        created.id, name="external.safetensors",
    )
    assert attached.loras[0].name == "external.safetensors"


# ---- tool integration -----------------------------------------------

@pytest.mark.asyncio
async def test_comfy_image_tool_passes_character_loras_to_workflow(
    tmp_path: Path,
) -> None:
    """Spot-check: the tool copies ``character.loras`` into
    ``PromptSpec.loras`` so the builder ends up injecting LoraLoader
    nodes. We mock the comfy client so no network hits occur."""
    from kokoro_link.contracts.tool import ToolContext
    from kokoro_link.infrastructure.tools.comfyui.generator import (
        ComfyPortraitGenerator,
    )
    from kokoro_link.infrastructure.tools.comfyui.tool import ComfyImageTool

    class _Client:
        def __init__(self) -> None:
            self.captured_prompt: dict | None = None

        async def queue_prompt(self, prompt: dict) -> str:
            self.captured_prompt = prompt
            return "pid"

        async def wait_for_completion(self, prompt_id: str) -> dict:
            return {"outputs": {}}

        async def download_image(
            self, *, filename: str, subfolder: str, folder_type: str,
        ) -> bytes:
            return b""

    client = _Client()
    builder = WorkflowBuilder(DEFAULT_WORKFLOW_FILE)
    from tests.unit._image_provider_stub import StaticActiveImageProvider
    tool = ComfyImageTool(
        image_provider=StaticActiveImageProvider(
            ComfyPortraitGenerator(
                client=client,  # type: ignore[arg-type]
                workflow_builder=builder,
            ),
        ),
        uploads_dir=tmp_path,
        object_storage=InMemoryObjectStorage(public_base_url="/uploads"),
    )
    character = Character.create(
        name="Y", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="n", affection=0, fatigue=0, trust=0, energy=100,
        ),
        loras=(CharacterLora(name="kokkoro.safetensors", strength=0.6),),
    )

    # Tool returns failure because save_images returns empty — we only
    # care about whether the workflow sent to ComfyUI has the LoRA.
    result = await tool.invoke(ToolContext(
        character=character,
        arguments={"positive": "1girl"},
    ))

    assert result.ok is False  # no images produced by our stub
    assert client.captured_prompt is not None
    assert "100" in client.captured_prompt
    assert client.captured_prompt["100"]["inputs"]["lora_name"] == "kokkoro.safetensors"
    assert client.captured_prompt["100"]["inputs"]["strength_model"] == 0.6
