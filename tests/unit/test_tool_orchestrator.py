"""BDD for ``ToolOrchestrator``.

The orchestrator is the only legal entry point for running a tool on
behalf of a character. It must:

- Refuse tools not in ``character.allowed_tools`` (→ DENIED).
- Refuse tools that aren't registered (→ DENIED).
- Swallow adapter exceptions into ``ToolResult.failure`` so the chat
  loop never crashes on a bad tool.
- Write a ``ToolInvocation`` audit row for every attempt (pending →
  success / failed / denied), so the operator UI can see what happened.
"""

from __future__ import annotations

from typing import Any, Mapping

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.contracts.tool import ToolContext, ToolPort
from kokoro_link.domain.entities.character import DEFAULT_ALLOWED_TOOLS
from kokoro_link.domain.entities.tool_invocation import (
    STATUS_DENIED,
    STATUS_FAILED,
    STATUS_SUCCESS,
)
from kokoro_link.domain.value_objects.tool_call import (
    ToolAttachment,
    ToolCall,
    ToolResult,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_tool_invocations import (
    InMemoryToolInvocationRepository,
)
from kokoro_link.infrastructure.tools.fake_tools import EchoTool, FakeImageTool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry


class _CrashingTool(ToolPort):
    name: str = "crasher"
    description: str = "always raises"
    parameters_schema: Mapping[str, Any] = {"type": "object"}

    async def invoke(self, ctx: ToolContext) -> ToolResult:
        raise RuntimeError("boom")


async def _seed_character(
    service: CharacterService, *, allowed_tools: list[str] | None = None,
):
    await service.create_character(
        CreateCharacterRequest(
            name="Yuki",
            allowed_tools=allowed_tools or [],
        ),
    )
    chars = await service.list_characters()
    entity = await service._repository.get(chars[0].id)  # type: ignore[attr-defined]
    assert entity is not None
    return entity


def _build(
    tools: list[ToolPort] | None = None,
) -> tuple[ToolOrchestrator, InMemoryToolInvocationRepository, CharacterService]:
    registry = InMemoryToolRegistry(tools or [EchoTool(), FakeImageTool()])
    repo = InMemoryToolInvocationRepository()
    orchestrator = ToolOrchestrator(
        registry=registry, invocation_repository=repo,
    )
    service = CharacterService(InMemoryCharacterRepository())
    return orchestrator, repo, service


@pytest.mark.asyncio
async def test_new_character_defaults_to_production_tools() -> None:
    _, _, service = _build()

    await service.create_character(CreateCharacterRequest(name="Yuki"))
    chars = await service.list_characters()
    entity = await service._repository.get(chars[0].id)  # type: ignore[attr-defined]

    assert entity is not None
    assert entity.allowed_tools == DEFAULT_ALLOWED_TOOLS


@pytest.mark.asyncio
async def test_allowed_tool_runs_and_logs_success() -> None:
    orchestrator, repo, service = _build()
    character = await _seed_character(service, allowed_tools=["echo"])

    invocation, result = await orchestrator.execute(
        character=character,
        call=ToolCall(name="echo", arguments={"text": "hi"}),
    )

    assert result.ok is True
    assert result.output_text == "echo: hi"
    assert invocation.status == STATUS_SUCCESS
    logs = await repo.list_for_character(character.id)
    assert len(logs) == 1
    assert logs[0].status == STATUS_SUCCESS


@pytest.mark.asyncio
async def test_disallowed_tool_is_denied_and_logged() -> None:
    orchestrator, repo, service = _build()
    character = await _seed_character(service, allowed_tools=[])

    invocation, result = await orchestrator.execute(
        character=character,
        call=ToolCall(name="echo", arguments={"text": "hi"}),
    )

    assert result.ok is False
    assert invocation.status == STATUS_DENIED
    logs = await repo.list_for_character(character.id)
    assert len(logs) == 1
    assert logs[0].status == STATUS_DENIED


@pytest.mark.asyncio
async def test_unknown_tool_is_denied() -> None:
    orchestrator, repo, service = _build()
    # Allow-list includes a ghost tool name that isn't registered.
    character = await _seed_character(service, allowed_tools=["ghost"])

    _, result = await orchestrator.execute(
        character=character,
        call=ToolCall(name="ghost", arguments={}),
    )

    assert result.ok is False
    assert "not registered" in (result.error or "")


@pytest.mark.asyncio
async def test_crashing_tool_is_swallowed_as_failure() -> None:
    orchestrator, repo, service = _build(tools=[_CrashingTool()])
    character = await _seed_character(service, allowed_tools=["crasher"])

    invocation, result = await orchestrator.execute(
        character=character,
        call=ToolCall(name="crasher"),
    )

    assert result.ok is False
    assert invocation.status == STATUS_FAILED
    assert "tool crashed" in (result.error or "")


@pytest.mark.asyncio
async def test_fake_image_tool_returns_attachment() -> None:
    orchestrator, repo, service = _build()
    character = await _seed_character(service, allowed_tools=["fake_image"])

    invocation, result = await orchestrator.execute(
        character=character,
        call=ToolCall(name="fake_image", arguments={"scene": "窗邊"}),
    )

    assert result.ok is True
    assert len(result.attachments) == 1
    att: ToolAttachment = result.attachments[0]
    assert att.kind == "image"
    assert att.url.startswith("/uploads/")
    # Audit row mirrors the attachment URL so the admin page can link out.
    assert invocation.attachment_urls == (att.url,)


@pytest.mark.asyncio
async def test_registry_filters_per_character_allowed_tools() -> None:
    registry = InMemoryToolRegistry([EchoTool(), FakeImageTool()])
    _, _, service = _build()
    char_none = await _seed_character(service, allowed_tools=[])
    # A second character in the same in-memory service has both.
    await service.create_character(
        CreateCharacterRequest(
            name="With", allowed_tools=["echo", "fake_image"],
        ),
    )
    chars = await service.list_characters()
    with_both = await service._repository.get(  # type: ignore[attr-defined]
        [c for c in chars if c.name == "With"][0].id,
    )
    assert with_both is not None

    assert registry.list_for_character(char_none) == []
    names = [t.name for t in registry.list_for_character(with_both)]
    assert names == ["echo", "fake_image"]
