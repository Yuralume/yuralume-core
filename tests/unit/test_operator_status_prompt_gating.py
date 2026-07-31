"""The owner's "current status" must not be attributed to another sender.

An external channel with an open (or multi-entry) sender allowlist can carry
several different humans into the same account. ``MessagingDispatcher`` marks
those turns with ``operator_persona_enabled=False`` so nothing the turn says
is written back onto the account owner's persona.

The SA-series status injection (D4) reads the *owner's* ``OperatorProfile``,
which ``ChatService`` resolves from the character's owner — not from whoever
actually typed. Rendering it on such a turn tells the character that the
person currently talking is "在醫院陪家人": wrong attribution, and it leaks
the owner's whereabouts to a stranger.

These tests pin the gate at every prompt-build seam of a player-facing turn:
the buffered reply, the streaming reply, every tool hop, and the novelty
retry.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.novelty_gate import NoveltyGateContext, NoveltyVerdict
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import Conversation
from kokoro_link.domain.entities.operator_profile import OperatorProfile
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
from kokoro_link.infrastructure.tools.fake_tools import EchoTool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry

_OWNER_STATUS = "在醫院陪家人"
_STATUS_SET_AT = datetime(2026, 7, 30, 21, 20, tzinfo=timezone.utc)


class _CapturingModel(ChatModelPort):
    """Records every prompt it is handed; optionally replays a script."""

    supports_vision = False
    prefers_public_image_urls = False

    def __init__(self, provider_id: str = "fake", replies: list[str] | None = None) -> None:
        self.provider_id = provider_id
        self._replies = list(replies or [])
        self.prompts: list[str] = []

    def _next_reply(self) -> str:
        if self._replies:
            return self._replies.pop(0)
        return "我在這裡。"

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
    """Always returns the *account owner's* profile — which is exactly the
    trap: on a shared channel the typist is not the owner."""

    def __init__(self) -> None:
        self.profile = OperatorProfile(
            id="owner-1",
            display_name="Alex",
            current_status=_OWNER_STATUS,
            current_status_set_at=_STATUS_SET_AT,
        )

    async def get_for_user(self, user_id: str) -> OperatorProfile:
        return self.profile

    async def get_current(self) -> OperatorProfile:
        return self.profile


class _SpyPromptBuilder(DefaultPromptContextBuilder):
    """Real rendering, plus the kwargs each call site handed over."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict] = []
        self.prompts: list[str] = []

    def build(self, **kwargs) -> str:  # noqa: ANN003
        self.calls.append(dict(kwargs))
        prompt = super().build(**kwargs)
        self.prompts.append(prompt)
        return prompt


class _AlwaysFailingNoveltyGate:
    """Forces the post-generation retry hop so its prompt is inspected too."""

    async def evaluate(
        self, context: NoveltyGateContext, *, character: Character,
    ) -> NoveltyVerdict:
        return NoveltyVerdict(passes=False, lacks_novelty=True, feedback="再具體一點")


def _assert_no_owner_status(prompt: str) -> None:
    assert _OWNER_STATUS not in prompt
    assert "目前狀態" not in prompt
    assert "自述的近況" not in prompt


def _build_service(
    *,
    model: _CapturingModel,
    prompt_builder: DefaultPromptContextBuilder | None = None,
    with_tools: bool = False,
    novelty_gate: object | None = None,
) -> tuple[ChatService, CharacterService, InMemoryCharacterRepository]:
    characters = InMemoryCharacterRepository()
    conversations = InMemoryConversationRepository()
    memories = InMemoryMemoryRepository()
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
        memory_repository=memories,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder or DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        operator_profile_service=_OwnerProfileService(),
        tool_registry=tool_registry,
        tool_orchestrator=orchestrator,
        novelty_gate=novelty_gate,
        novelty_gate_enabled=novelty_gate is not None,
        reply_quality_gate_risk_threshold=0.0,
    )
    character_service = CharacterService(
        characters,
        conversation_repository=conversations,
        memory_repository=memories,
    )
    return service, character_service, characters


@pytest.mark.asyncio
async def test_owner_status_reaches_the_prompt_on_an_owner_turn() -> None:
    """Control: the single-sender / web case still gets the D4 injection."""
    model = _CapturingModel()
    chat, characters, _ = _build_service(model=model)
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))

    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="在幹嘛",
            operator_persona_enabled=True,
        ),
    )

    assert model.prompts
    assert _OWNER_STATUS in model.prompts[-1]


@pytest.mark.asyncio
async def test_owner_status_is_withheld_from_a_multi_sender_turn() -> None:
    model = _CapturingModel()
    chat, characters, _ = _build_service(model=model)
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))

    await chat.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="在幹嘛",
            operator_persona_enabled=False,
        ),
    )

    assert model.prompts
    for prompt in model.prompts:
        _assert_no_owner_status(prompt)


@pytest.mark.asyncio
async def test_owner_status_is_withheld_on_the_streaming_path() -> None:
    model = _CapturingModel()
    chat, characters, _ = _build_service(model=model)
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))

    token_stream, finalizer = await chat.send_message_stream(
        SendChatMessageRequest(
            character_id=created.id,
            message="在幹嘛",
            operator_persona_enabled=False,
        ),
    )
    chunks = [chunk async for chunk in token_stream]
    await finalizer.finish("".join(chunks))

    assert model.prompts
    for prompt in model.prompts:
        _assert_no_owner_status(prompt)


@pytest.mark.asyncio
async def test_streaming_owner_turn_still_gets_the_status() -> None:
    model = _CapturingModel()
    chat, characters, _ = _build_service(model=model)
    created = await characters.create_character(CreateCharacterRequest(name="Mio"))

    token_stream, finalizer = await chat.send_message_stream(
        SendChatMessageRequest(
            character_id=created.id,
            message="在幹嘛",
            operator_persona_enabled=True,
        ),
    )
    chunks = [chunk async for chunk in token_stream]
    await finalizer.finish("".join(chunks))

    assert model.prompts
    assert _OWNER_STATUS in model.prompts[-1]


@pytest.mark.asyncio
async def test_owner_status_is_withheld_on_every_tool_hop_and_retry() -> None:
    """The tool cycle builds a fresh prompt per hop plus one for the novelty
    retry — a gate applied to only the first would leak on the rest."""
    model = _CapturingModel(
        replies=[
            '```json\n{"tool": "echo", "args": {"text": "哈囉"}}\n```',
            "我剛剛回聲了一下。",
            "換個說法：我剛剛把你的話回聲了一次。",
        ],
    )
    spy_builder = _SpyPromptBuilder()
    chat, character_service, characters = _build_service(
        model=model,
        prompt_builder=spy_builder,
        with_tools=True,
        novelty_gate=_AlwaysFailingNoveltyGate(),
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", allowed_tools=["echo"]),
    )
    character = await characters.get(created.id)
    assert character is not None
    conversation = Conversation.start(character_id=character.id)
    owner = OperatorProfile(
        id="owner-1",
        display_name="Alex",
        current_status=_OWNER_STATUS,
        current_status_set_at=_STATUS_SET_AT,
    )

    await chat._generate_reply_with_tools(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="回聲一下",
        active_goals=[],
        current_activity=None,
        upcoming_activities=[],
        now=_STATUS_SET_AT + timedelta(minutes=20),
        idle_minutes=5,
        model=model,
        model_id=None,
        operator=owner,
        operator_persona_enabled=False,
    )

    # More than one build happened (tool hop + wrap-up + novelty retry).
    assert len(spy_builder.prompts) > 1
    for call in spy_builder.calls:
        assert call["include_operator_status"] is False
    for prompt in spy_builder.prompts:
        _assert_no_owner_status(prompt)
