"""The SSE route's cleanup must cover its own first frame (C3).

The streaming chat route opens by yielding the ``conversation_id`` frame so the
frontend can bind the conversation before a single token arrives. That yield
used to sit *above* the ``try``/``finally`` that releases the turn — which meant
a client that went away while exactly that frame was in flight closed the
generator at a suspension point no cleanup covered:

* the conversation lease stayed claimed until its 180s TTL, so the player's own
  retry came back ``conversation_busy``;
* the action charge stayed reserved until the stale-reservation sweeper ran;
* and ``active_turns`` — an in-process integer with no TTL and no sweeper —
  stayed above zero **for the life of the process**, so every subsequent drain
  spent its whole 180s budget waiting for a turn that ended long ago before
  killing the replica anyway. GD's central promise, silently voided by a
  disconnect at the one moment disconnects cluster: hitting send and
  immediately navigating away.

The route is driven directly rather than through ``TestClient`` because the
event under test is "the consumer stops consuming at frame one", which is
``aclose()`` on the response's body iterator — precisely what Starlette does to
an abandoned stream, and not something an HTTP round trip can express.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

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
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

pytestmark = pytest.mark.asyncio

_USER_ID = "default"


class _CountingBilling(NullActionBillingService):
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


class _Fixture:
    """One replica's chat wiring, plus the container shape the route reads."""

    def __init__(self) -> None:
        self.characters = InMemoryCharacterRepository()
        self.conversations = InMemoryConversationRepository()
        self.memories = InMemoryMemoryRepository()
        self.drain = DrainState()
        self.billing = _CountingBilling()
        self.character_service = CharacterService(
            self.characters,
            conversation_repository=self.conversations,
            memory_repository=self.memories,
        )
        registry = InMemoryChatModelRegistry(default_provider_id="fake")
        registry.register(FakeChatModel("fake"))
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

    async def seed_character(self) -> str:
        created = await self.character_service.create_character(
            CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
        )
        return created.id


async def _open_stream(fixture: _Fixture, character_id: str, message: str):  # noqa: ANN202
    return await send_chat_message_stream(
        payload=SendChatMessageRequest(
            character_id=character_id, message=message, provider_id="fake",
        ),
        container=fixture.container(),
        current_user_id=_USER_ID,
        _drain_gate=None,
    )


async def test_a_disconnect_on_the_first_frame_releases_the_whole_turn() -> None:
    fixture = _Fixture()
    character_id = await fixture.seed_character()

    response = await _open_stream(fixture, character_id, "第一幀就跑掉")
    frames = response.body_iterator
    first = await frames.__anext__()

    # Frame one is the conversation binding, and the turn is fully in flight:
    # charge opened, conversation claimed, drain slot taken.
    assert json.loads(first.removeprefix("data: ").strip())["conversation_id"]
    assert fixture.billing.begins == 1
    assert fixture.drain.active_turns == 1

    # The client goes away right here. Starlette closes the body iterator, which
    # throws ``GeneratorExit`` into the generator at that first yield.
    await frames.aclose()

    assert fixture.drain.active_turns == 0
    assert fixture.billing.releases == 1
    assert fixture.billing.settles == 0


async def test_the_conversation_is_free_again_immediately_after_that() -> None:
    """The lease half of the same leak, asserted where a player would feel it.

    A stranded lease is not visible as a counter; it is visible as the player's
    very next message coming back ``conversation_busy`` for the next three
    minutes. So the assertion is that message going through.
    """
    fixture = _Fixture()
    character_id = await fixture.seed_character()

    response = await _open_stream(fixture, character_id, "送出後立刻離開")
    frames = response.body_iterator
    first = await frames.__anext__()
    conversation_id = json.loads(
        first.removeprefix("data: ").strip(),
    )["conversation_id"]
    await frames.aclose()

    retry = await fixture.chat_service.send_message(
        SendChatMessageRequest(
            character_id=character_id,
            conversation_id=conversation_id,
            message="我回來了",
            provider_id="fake",
        ),
        current_user_id=_USER_ID,
    )

    assert retry.assistant_message is not None
    assert fixture.drain.active_turns == 0


async def test_a_fully_consumed_stream_still_settles_normally() -> None:
    """The regression guard: moving the frame must not change the happy path."""
    fixture = _Fixture()
    character_id = await fixture.seed_character()

    response = await _open_stream(fixture, character_id, "好好講完")
    frames = [chunk async for chunk in response.body_iterator]

    assert frames[0].startswith("data: ")
    assert "conversation_id" in frames[0]
    assert frames[-1] == "data: [DONE]\n\n"
    assert any('"done": true' in f for f in frames)
    assert fixture.billing.settles == 1
    assert fixture.billing.releases == 1  # the finalizer's idempotent no-op
    assert fixture.drain.active_turns == 0
