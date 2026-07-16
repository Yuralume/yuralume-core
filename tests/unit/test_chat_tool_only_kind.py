"""ChatService tags tool-only assistant turns with ``MessageKind.TOOL_ONLY``.

When the second-hop model reply is empty (e.g. the character already
"spoke" via an attachment and has nothing to add), the persisted
assistant message should carry ``kind=TOOL_ONLY`` so downstream
summarisers skip it. When the second-hop reply has text, the message
stays ``kind=CHAT`` even though it also carries an attachment.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.conversation import MessageKind
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_tool_invocations import (
    InMemoryToolInvocationRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine
from kokoro_link.infrastructure.tools.fake_tools import EchoTool, FakeImageTool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry


class _ScriptedModel(ChatModelPort):
    def __init__(self, replies: list[str]) -> None:
        self.provider_id = "fake"
        self._replies = list(replies)
        self.calls: list[Any] = []

    supports_vision = False

    async def generate(self, prompt: str, **_: Any) -> str:
        self.calls.append(prompt)
        if not self._replies:
            return ""
        return self._replies.pop(0)

    async def generate_stream(self, prompt: str, **_: Any) -> AsyncIterator[str]:
        text = await self.generate(prompt)

        async def _iter() -> AsyncIterator[str]:
            yield text

        return _iter()


def _build() -> tuple[ChatService, CharacterService, InMemoryConversationRepository]:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    invocation_repository = InMemoryToolInvocationRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    # Two replies: first a tool call JSON, second an empty string so the
    # assistant turn ends up text-less with only the tool attachment.
    registry.register(_ScriptedModel([
        '```json\n{"tool": "fake_image", "args": {"scene": "x"}}\n```',
        "",
    ]))
    tool_registry = InMemoryToolRegistry([
        EchoTool(),
        FakeImageTool(url="/uploads/stub/fake.png"),
    ])
    orchestrator = ToolOrchestrator(
        registry=tool_registry,
        invocation_repository=invocation_repository,
    )
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        tool_registry=tool_registry,
        tool_orchestrator=orchestrator,
    )
    character_service = CharacterService(character_repository)
    return chat_service, character_service, conversation_repository


async def _seed(chars: CharacterService) -> str:
    created = await chars.create_character(
        CreateCharacterRequest(name="Yuki", allowed_tools=["fake_image"]),
    )
    return created.id


@pytest.mark.asyncio
async def test_empty_second_hop_with_attachment_is_tool_only() -> None:
    chat, chars, convos = _build()
    character_id = await _seed(chars)

    await chat.send_message(SendChatMessageRequest(
        character_id=character_id, message="傳張照片",
    ))

    conversation = await convos.latest_for_character(character_id)
    assert conversation is not None
    assistant_messages = [m for m in conversation.messages if m.role.value == "assistant"]
    assert len(assistant_messages) == 1
    msg = assistant_messages[0]
    assert msg.content.strip() == ""
    assert len(msg.attachments) == 1
    assert msg.kind is MessageKind.TOOL_ONLY


@pytest.mark.asyncio
async def test_non_empty_second_hop_with_attachment_stays_chat() -> None:
    """Second hop produces narrative text alongside the attachment — the
    assistant turn is conversational, so it should keep ``kind=CHAT``."""
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    invocation_repository = InMemoryToolInvocationRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(_ScriptedModel([
        '```json\n{"tool": "fake_image", "args": {"scene": "x"}}\n```',
        "拍好了，希望你喜歡～",
    ]))
    tool_registry = InMemoryToolRegistry([
        EchoTool(),
        FakeImageTool(url="/uploads/stub/fake.png"),
    ])
    orchestrator = ToolOrchestrator(
        registry=tool_registry,
        invocation_repository=invocation_repository,
    )
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        tool_registry=tool_registry,
        tool_orchestrator=orchestrator,
    )
    character_service = CharacterService(character_repository)
    character_id = await _seed(character_service)

    await chat_service.send_message(SendChatMessageRequest(
        character_id=character_id, message="傳張照片",
    ))

    conversation = await conversation_repository.latest_for_character(character_id)
    assert conversation is not None
    assistant_messages = [m for m in conversation.messages if m.role.value == "assistant"]
    assert assistant_messages[0].content.strip() != ""
    assert len(assistant_messages[0].attachments) == 1
    assert assistant_messages[0].kind is MessageKind.CHAT


@pytest.mark.asyncio
async def test_plain_text_reply_is_chat_kind() -> None:
    """Regression guard: plain text (no tool, no attachment) stays CHAT."""
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(_ScriptedModel(["嗨嗨～今天過得怎麼樣？"]))
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
    )
    character_service = CharacterService(character_repository)
    character_id = await _seed(character_service)

    await chat_service.send_message(SendChatMessageRequest(
        character_id=character_id, message="hi",
    ))

    conversation = await conversation_repository.latest_for_character(character_id)
    assert conversation is not None
    assistant_messages = [m for m in conversation.messages if m.role.value == "assistant"]
    assert assistant_messages[0].kind is MessageKind.CHAT
