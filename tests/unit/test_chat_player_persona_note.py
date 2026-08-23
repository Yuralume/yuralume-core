"""PP2 — the declared setting reaches the chat prompt, and only there.

The gate is the same one the operator persona rides: a turn marked
``operator_persona_enabled=False`` may have been typed by someone other
than the account owner, so the owner's declared setting must not be
staged as the current speaker's. The buffered path, the streaming path
and every tool hop each build their own prompt, so each is pinned.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.player_persona_note_service import (
    PlayerPersonaNoteService,
)
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.prompt.player_persona_note_lines import (
    PLAYER_PERSONA_NOTE_HEADER,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_player_persona_notes import (
    InMemoryPlayerPersonaNoteRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_tool_invocations import (
    InMemoryToolInvocationRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine
from kokoro_link.infrastructure.tools.fake_tools import EchoTool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry

_NOTE = "我是超能力者，能聽見別人心裡的聲音"


class _CapturingModel(ChatModelPort):
    supports_vision = False
    prefers_public_image_urls = False

    def __init__(self, replies: list[str] | None = None) -> None:
        self.provider_id = "fake"
        self._replies = list(replies or [])
        self.prompts: list[str] = []

    def _next_reply(self) -> str:
        return self._replies.pop(0) if self._replies else "我在這裡。"

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        self.prompts.append(prompt)
        return self._next_reply()

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        self.prompts.append(prompt)
        yield self._next_reply()

    async def list_models(self) -> list[str]:
        return [self.provider_id]


class _OwnerProfileService:
    def __init__(self) -> None:
        self.profile = OperatorProfile(id="owner-1", display_name="Alex")

    async def get_for_user(self, user_id: str) -> OperatorProfile:
        return self.profile

    async def get_current(self) -> OperatorProfile:
        return self.profile


def _build_service(
    *,
    model: _CapturingModel,
    notes: InMemoryPlayerPersonaNoteRepository,
    with_tools: bool = False,
) -> tuple[ChatService, CharacterService]:
    characters = InMemoryCharacterRepository()
    conversations = InMemoryConversationRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(model)
    tool_registry = InMemoryToolRegistry([EchoTool()]) if with_tools else None
    orchestrator = (
        ToolOrchestrator(
            registry=tool_registry,
            invocation_repository=InMemoryToolInvocationRepository(),
        )
        if tool_registry is not None else None
    )
    service = ChatService(
        character_repository=characters,
        conversation_repository=conversations,
        memory_repository=InMemoryMemoryRepository(),
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        operator_profile_service=_OwnerProfileService(),
        player_persona_note_repository=notes,
        tool_registry=tool_registry,
        tool_orchestrator=orchestrator,
    )
    character_service = CharacterService(
        characters,
        conversation_repository=conversations,
        memory_repository=InMemoryMemoryRepository(),
    )
    return service, character_service


async def _seed_note(
    notes: InMemoryPlayerPersonaNoteRepository, character_id: str,
) -> None:
    await PlayerPersonaNoteService(notes).set(
        character_id=character_id, operator_id="owner-1", note=_NOTE,
    )


@pytest.mark.asyncio
async def test_declared_note_reaches_the_buffered_prompt() -> None:
    model = _CapturingModel()
    notes = InMemoryPlayerPersonaNoteRepository()
    chat, character_service = _build_service(model=model, notes=notes)
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio"),
    )
    await _seed_note(notes, created.id)

    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="在幹嘛",
            operator_persona_enabled=True,
        ),
    )

    assert model.prompts
    assert PLAYER_PERSONA_NOTE_HEADER in model.prompts[-1]
    assert _NOTE in model.prompts[-1]


@pytest.mark.asyncio
async def test_no_declared_note_leaves_the_prompt_untouched() -> None:
    model = _CapturingModel()
    notes = InMemoryPlayerPersonaNoteRepository()
    chat, character_service = _build_service(model=model, notes=notes)
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio"),
    )

    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="在幹嘛",
            operator_persona_enabled=True,
        ),
    )

    assert model.prompts
    assert PLAYER_PERSONA_NOTE_HEADER not in model.prompts[-1]


@pytest.mark.asyncio
async def test_note_is_withheld_when_the_persona_gate_is_closed() -> None:
    model = _CapturingModel()
    notes = InMemoryPlayerPersonaNoteRepository()
    chat, character_service = _build_service(model=model, notes=notes)
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio"),
    )
    await _seed_note(notes, created.id)

    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="在幹嘛",
            operator_persona_enabled=False,
        ),
    )

    assert model.prompts
    for prompt in model.prompts:
        assert PLAYER_PERSONA_NOTE_HEADER not in prompt
        assert _NOTE not in prompt


@pytest.mark.asyncio
async def test_streaming_path_carries_the_same_gate() -> None:
    notes = InMemoryPlayerPersonaNoteRepository()

    async def run(enabled: bool) -> list[str]:
        model = _CapturingModel()
        chat, character_service = _build_service(model=model, notes=notes)
        created = await character_service.create_character(
            CreateCharacterRequest(name="Mio"),
        )
        await _seed_note(notes, created.id)
        token_stream, finalizer = await chat.send_message_stream(
            SendChatMessageRequest(
                character_id=created.id,
                message="在幹嘛",
                operator_persona_enabled=enabled,
            ),
        )
        chunks = [chunk async for chunk in token_stream]
        await finalizer.finish("".join(chunks))
        return model.prompts

    assert any(_NOTE in prompt for prompt in await run(True))
    assert all(_NOTE not in prompt for prompt in await run(False))


@pytest.mark.asyncio
async def test_every_tool_hop_carries_the_note() -> None:
    """A gate or an injection applied to only the first hop is a leak in
    one direction and a hole in the other."""
    model = _CapturingModel(
        replies=[
            '```json\n{"tool": "echo", "args": {"text": "哈囉"}}\n```',
            "我剛剛回聲了一下。",
        ],
    )
    notes = InMemoryPlayerPersonaNoteRepository()
    chat, character_service = _build_service(
        model=model, notes=notes, with_tools=True,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", allowed_tools=["echo"]),
    )
    await _seed_note(notes, created.id)

    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="回聲一下",
            operator_persona_enabled=True,
        ),
    )

    assert len(model.prompts) > 1
    for prompt in model.prompts:
        assert _NOTE in prompt
