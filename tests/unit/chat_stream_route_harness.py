"""One replica's chat wiring, driven through the SSE route itself.

Shared by every test that has to observe a *client disconnect*: the event is
``aclose()`` on the response's body iterator — precisely what Starlette does to
an abandoned stream, and not something an HTTP round trip through ``TestClient``
can express. So these tests call the route function directly and drive the
iterator by hand.

Kept out of any single test module because the disconnect story now spans
several: the first-frame hole (C3) and detached completion both need the same
wiring, and a second copy is a second thing to forget to update.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from types import SimpleNamespace

from kokoro_link.api.routes import chat as chat_route
from kokoro_link.api.routes.chat import send_chat_message_stream
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.chat_turn_lease import ChatTurnLease
from kokoro_link.application.services.cloud_action_billing_service import (
    NullActionBillingService,
)
from kokoro_link.application.services.drain_state import DrainState
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
    InMemoryBackgroundCoordinatorLease,
)
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
from kokoro_link.infrastructure.tools.fake_tools import FakeImageTool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry

USER_ID = "default"
FAKE_IMAGE_URL = "/uploads/stub/harness.png"


class CountingBilling(NullActionBillingService):
    """Charges nothing, but remembers what was opened and how it closed."""

    def __init__(self) -> None:
        self.begins = 0
        self.releases = 0
        self.settles = 0

    async def begin(self, action_key: str, **kwargs):  # noqa: ANN003
        self.begins += 1
        return await super().begin(action_key, **kwargs)

    async def release(self, handle, **kwargs):  # noqa: ANN001, ANN003
        self.releases += 1
        return await super().release(handle, **kwargs)

    async def settle(self, handle, **kwargs):  # noqa: ANN001, ANN003
        self.settles += 1
        return await super().settle(handle, **kwargs)


class GatedStreamModel(FakeChatModel):
    """Token streaming that stops half-way until the test says otherwise.

    ``_stream_capturing`` pulls the first chunk eagerly (so an image rejection
    surfaces before any token reaches the wire), which means ``chunks[0]`` is
    already buffered by the time the route returns. The gate therefore sits
    between chunk one and chunk two: exactly "the player has seen part of the
    reply and now walks away".
    """

    def __init__(
        self,
        provider_id: str = "fake",
        *,
        chunks: Sequence[str] = ("前半段。", "後半段。"),
        stall: bool = False,
    ) -> None:
        super().__init__(provider_id)
        self._chunks = tuple(chunks)
        self._stall = stall
        self.gate = asyncio.Event()

    @property
    def full_text(self) -> str:
        return "".join(self._chunks)

    async def generate_stream(  # type: ignore[override]
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        yield self._chunks[0]
        if self._stall:
            # Never opened by anyone — the "upstream wedged forever" case the
            # detach watchdog exists for.
            await asyncio.Event().wait()
        await self.gate.wait()
        for chunk in self._chunks[1:]:
            yield chunk


class GatedToolModel(ChatModelPort):
    """Scripted tool cycle whose *second* hop waits for the test's gate.

    The tool path never streams real tokens (hop one may be a JSON tool call),
    so the disconnect window is the tool-activity frames — which is where the
    gate puts it.
    """

    provider_id = "fake"
    supports_vision = False

    def __init__(self, replies: Sequence[str]) -> None:
        self._replies = list(replies)
        self.calls = 0
        self.gate = asyncio.Event()

    async def generate(self, prompt: str, **kwargs) -> str:  # noqa: ANN003
        self.calls += 1
        if self.calls > 1:
            await self.gate.wait()
        return self._replies.pop(0) if self._replies else "（沒有更多腳本）"

    async def generate_stream(  # noqa: D102
        self, prompt: str, **kwargs,  # noqa: ANN003
    ) -> AsyncIterator[str]:
        text = await self.generate(prompt)

        async def _iter() -> AsyncIterator[str]:
            yield text

        return _iter()

    async def list_models(self) -> list[str]:
        return [self.provider_id]


class StreamRouteFixture:
    """Everything the SSE route reads, wired in memory."""

    def __init__(self, *, model: ChatModelPort | None = None, tools: bool = False) -> None:
        self.relays: list = []
        self.characters = InMemoryCharacterRepository()
        self.conversations = InMemoryConversationRepository()
        self.memories = InMemoryMemoryRepository()
        self.invocations = InMemoryToolInvocationRepository()
        self.drain = DrainState()
        self.billing = CountingBilling()
        self.model = model or FakeChatModel("fake")
        self.character_service = CharacterService(
            self.characters,
            conversation_repository=self.conversations,
            memory_repository=self.memories,
        )
        registry = InMemoryChatModelRegistry(default_provider_id="fake")
        registry.register(self.model)

        tool_registry = orchestrator = None
        if tools:
            tool_registry = InMemoryToolRegistry([FakeImageTool(url=FAKE_IMAGE_URL)])
            orchestrator = ToolOrchestrator(
                registry=tool_registry,
                invocation_repository=self.invocations,
            )

        self.chat_service = ChatService(
            character_repository=self.characters,
            conversation_repository=self.conversations,
            memory_repository=self.memories,
            post_turn_processor=NullPostTurnProcessor(),
            prompt_context_builder=DefaultPromptContextBuilder(),
            model_registry=registry,
            state_engine=SimpleStateEngine(),
            turn_lease=ChatTurnLease(InMemoryBackgroundCoordinatorLease()),
            drain_state=self.drain,
            action_billing=self.billing,
            tool_registry=tool_registry,
            tool_orchestrator=orchestrator,
        )

    def container(self) -> SimpleNamespace:
        return SimpleNamespace(
            chat_service=self.chat_service,
            character_service=self.character_service,
            conversation_repository=self.conversations,
            drain_state=self.drain,
            turn_undo_service=None,
            object_storage=None,
            app_settings=None,
            operator_profile_repository=None,
        )

    async def seed_character(self, *, allowed_tools: Sequence[str] = ()) -> str:
        created = await self.character_service.create_character(
            CreateCharacterRequest(
                name="Mio",
                personality=["kind"],
                interests=[],
                allowed_tools=list(allowed_tools),
            ),
        )
        return created.id

    async def open_stream(self, character_id: str, message: str):  # noqa: ANN201
        """Call the route, keeping the relay it built in ``self.relays``.

        The relay is the only thing that can answer "did this disconnect
        actually detach the turn?". Every observable *result* of detaching —
        the message landing, the charge settling, the drain slot dropping — is
        also what a turn that simply finished on its own produces, so a test
        that asserts only those cannot tell a working ``finally`` from a
        missing one. Hence the reach-in: the route builds its relay privately
        and there is no other seam.
        """
        original = chat_route.TurnStreamRelay

        def _record(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
            relay = original(*args, **kwargs)
            self.relays.append(relay)
            return relay

        chat_route.TurnStreamRelay = _record  # type: ignore[assignment]
        try:
            return await send_chat_message_stream(
                payload=SendChatMessageRequest(
                    character_id=character_id, message=message, provider_id="fake",
                ),
                container=self.container(),
                current_user_id=USER_ID,
                _drain_gate=None,
            )
        finally:
            chat_route.TurnStreamRelay = original  # type: ignore[assignment]

    async def assistant_messages(self, character_id: str) -> list:
        conversation = await self.chat_service.get_latest_conversation(character_id)
        if conversation is None:
            return []
        return [m for m in conversation.messages if m.role == "assistant"]


def frame_payload(chunk: str) -> dict | str:
    """Decode one SSE line into its JSON body (``[DONE]`` stays a string)."""
    body = chunk.removeprefix("data: ").strip()
    if body == "[DONE]":
        return body
    return json.loads(body)
