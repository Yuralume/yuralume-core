"""起幕 in-scene turns: context, chips, and the idle clock (SC1-C).

Covers the two places chips are produced — the opening and every
in-scene chat turn — plus the three rules that keep the feature from
leaking into ordinary chat:

* a turn outside a scene makes **zero** extra LLM calls;
* a chips failure is invisible to the player, never a failed turn;
* every in-scene turn stamps the scene's idle clock, which is the only
  thing standing between an abandoned scene and SC1-E's timeout closer.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.story_scene_service import (
    StorySceneService,
)
from kokoro_link.contracts.story_scene import (
    StorySceneChipsContext,
    StorySceneChipsWriterPort,
    StorySceneMaterial,
    StorySceneMaterialProviderPort,
    StorySceneOpeningDraft,
    StorySceneOpenerPort,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_MANUAL,
    SCENE_LAYER_BEAT,
    SCENE_LAYER_SIDE_STORY,
    StorySceneSession,
)
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import (
    NullPostTurnProcessor,
)
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_scene_sessions import (
    InMemoryStorySceneSessionRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
TODAY = date(2026, 6, 1)


# ── doubles ──────────────────────────────────────────────────────────


class _RecordingChipsWriter(StorySceneChipsWriterPort):
    def __init__(
        self,
        actions: tuple[str, ...] = ("靠過去", "先不說話"),
        *,
        raises: Exception | None = None,
    ) -> None:
        self._actions = actions
        self._raises = raises
        self.contexts: list[StorySceneChipsContext] = []

    async def suggest_actions(
        self, context: StorySceneChipsContext,
    ) -> tuple[str, ...]:
        self.contexts.append(context)
        if self._raises is not None:
            raise self._raises
        return self._actions


class _PromptCapturingModel(FakeChatModel):
    def __init__(self, provider_id: str = "fake") -> None:
        super().__init__(provider_id)
        self.prompts: list[str] = []

    async def generate(self, prompt: str, **kwargs) -> str:  # noqa: ANN003
        self.prompts.append(prompt)
        return "她看著你，沒有立刻回答。"

    async def generate_stream(self, prompt: str, **kwargs):  # noqa: ANN003
        yield await self.generate(prompt, **kwargs)


class _StubOpener(StorySceneOpenerPort):
    async def write_opening(self, context):  # noqa: ANN001
        return StorySceneOpeningDraft(
            narration="風把話吹散了。",
            character_line="你怎麼還在這裡？",
            title="把話說完",
            location="頂樓天台",
            mood="欲言又止",
        )


class _StubMaterialProvider(StorySceneMaterialProviderPort):
    layer = SCENE_LAYER_SIDE_STORY

    async def resolve(self, character, *, today):  # noqa: ANN001
        return StorySceneMaterial(
            layer=SCENE_LAYER_SIDE_STORY,
            title="沒說完的那句",
            dramatic_question="她要承認自己練得不夠嗎？",
        )


# ── chat-turn fixture ────────────────────────────────────────────────


class _Fixture:
    def __init__(
        self,
        *,
        chips: _RecordingChipsWriter | None = None,
        wire_scene: bool = True,
    ) -> None:
        self.characters = InMemoryCharacterRepository()
        self.conversations = InMemoryConversationRepository()
        self.memories = InMemoryMemoryRepository()
        self.sessions = InMemoryStorySceneSessionRepository()
        self.chips = chips if chips is not None else _RecordingChipsWriter()
        self.model = _PromptCapturingModel()
        registry = InMemoryChatModelRegistry(default_provider_id="fake")
        registry.register(self.model)
        self.chat = ChatService(
            character_repository=self.characters,
            conversation_repository=self.conversations,
            memory_repository=self.memories,
            post_turn_processor=NullPostTurnProcessor(),
            prompt_context_builder=DefaultPromptContextBuilder(),
            model_registry=registry,
            state_engine=SimpleStateEngine(),
            story_scene_sessions=self.sessions if wire_scene else None,
            story_scene_chips_writer=self.chips if wire_scene else None,
        )
        self.character_service = CharacterService(
            self.characters,
            conversation_repository=self.conversations,
            memory_repository=self.memories,
        )

    async def character_id(self) -> str:
        created = await self.character_service.create_character(
            CreateCharacterRequest(name="Aki", personality=["倔強"], interests=[]),
        )
        return created.id

    async def open_scene(self, character_id: str) -> StorySceneSession:
        session = StorySceneSession.open_scene(
            character_id=character_id,
            conversation_id="conv-scene",
            source_layer=SCENE_LAYER_BEAT,
            title="把話說完",
            location="頂樓天台",
            mood="欲言又止",
            scene_type="conflict",
            dramatic_question="她要承認自己練得不夠嗎？",
            opened_at=NOW,
        )
        await self.sessions.add(session)
        return session


async def _drain(stream) -> str:  # noqa: ANN001
    return "".join([token async for token in stream])


# ── in-scene chat turns ──────────────────────────────────────────────


async def test_in_scene_turn_injects_the_scene_frame_into_the_prompt() -> None:
    fixture = _Fixture()
    character_id = await fixture.character_id()
    await fixture.open_scene(character_id)

    await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
    )

    prompt = fixture.model.prompts[-1]
    assert "【劇情場景進行中】" in prompt
    assert "頂樓天台" in prompt
    assert "她要承認自己練得不夠嗎？" in prompt


async def test_turn_outside_a_scene_leaves_the_prompt_alone() -> None:
    fixture = _Fixture()
    character_id = await fixture.character_id()

    await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
    )

    assert "劇情場景進行中" not in fixture.model.prompts[-1]


async def test_in_scene_turn_returns_chips() -> None:
    fixture = _Fixture()
    character_id = await fixture.character_id()
    await fixture.open_scene(character_id)

    reply = await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
    )

    assert [chip.text for chip in reply.suggested_actions] == [
        "靠過去", "先不說話",
    ]


async def test_chips_read_the_reply_that_just_landed() -> None:
    """Chips are about what to do *next*, so they see the fresh turn."""
    fixture = _Fixture()
    character_id = await fixture.character_id()
    await fixture.open_scene(character_id)

    await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
    )

    lines = fixture.chips.contexts[0].recent_lines
    assert any("玩家：我在。" == line for line in lines)
    assert any(line.startswith("Aki：") for line in lines)


async def test_turn_outside_a_scene_never_calls_the_chips_writer() -> None:
    """The scene feature must cost a plain chat turn exactly nothing."""
    fixture = _Fixture()
    character_id = await fixture.character_id()

    reply = await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )

    assert fixture.chips.contexts == []
    assert reply.suggested_actions == []


async def test_a_failing_chips_writer_does_not_fail_the_turn() -> None:
    fixture = _Fixture(
        chips=_RecordingChipsWriter(raises=RuntimeError("chips upstream down")),
    )
    character_id = await fixture.character_id()
    await fixture.open_scene(character_id)

    reply = await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
    )

    assert reply.assistant_message is not None
    assert reply.suggested_actions == []


async def test_in_scene_turn_stamps_the_idle_clock() -> None:
    fixture = _Fixture()
    character_id = await fixture.character_id()
    session = await fixture.open_scene(character_id)

    await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
    )

    live = await fixture.sessions.get(session.id)
    assert live is not None
    assert live.last_activity_at > session.last_activity_at


async def test_a_scene_closed_mid_turn_gets_no_chips() -> None:
    """The stamp doubles as the liveness check — one write, no extra read."""
    fixture = _Fixture()
    character_id = await fixture.character_id()
    session = await fixture.open_scene(character_id)
    await fixture.sessions.save(
        session.closed(reason=SCENE_CLOSE_MANUAL, at=NOW),
    )

    reply = await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
    )

    assert fixture.chips.contexts == []
    assert reply.suggested_actions == []


async def test_unwired_scene_runtime_is_the_pre_sc1c_chat_path() -> None:
    fixture = _Fixture(wire_scene=False)
    character_id = await fixture.character_id()

    reply = await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )

    assert reply.suggested_actions == []
    assert "劇情場景進行中" not in fixture.model.prompts[-1]


class _FakeExternalTurn:
    """Just enough of the LH2 execution port to drive one turn."""

    def __init__(self, conversations: InMemoryConversationRepository) -> None:
        self._conversations = conversations
        self.assistant_position = 0

    def stable_turn_id(self) -> str:
        return "external-turn-1"

    def heartbeat_interval_seconds(self) -> float:
        return 60.0

    async def heartbeat(self) -> None:
        return None

    async def persist_user_turn(self, conversation, message):  # noqa: ANN001
        await self._conversations.save(conversation.append(message))

    async def checkpoint_generated(self, snapshot) -> None:  # noqa: ANN001
        return None

    async def commit_assistant_turn(self, conversation, message):  # noqa: ANN001
        updated = conversation.append(message)
        self.assistant_position = len(updated.messages) - 1
        await self._conversations.save(updated)

    def post_turn_anchors(self):  # noqa: ANN201
        from types import SimpleNamespace

        return SimpleNamespace(assistant_position=self.assistant_position)


async def test_external_channel_turn_keeps_the_scene_alive_without_chips() -> None:
    """LINE plays the scene for real, but has nowhere to render a chip."""
    fixture = _Fixture()
    character_id = await fixture.character_id()
    session = await fixture.open_scene(character_id)

    await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
        external_turn=_FakeExternalTurn(fixture.conversations),
    )

    live = await fixture.sessions.get(session.id)
    assert live is not None
    assert live.last_activity_at > session.last_activity_at
    assert fixture.chips.contexts == []


async def test_external_channel_turn_still_gets_the_scene_frame() -> None:
    """The character must stay in the scene whichever channel it is on."""
    fixture = _Fixture()
    character_id = await fixture.character_id()
    await fixture.open_scene(character_id)

    await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
        external_turn=_FakeExternalTurn(fixture.conversations),
    )

    assert "【劇情場景進行中】" in fixture.model.prompts[-1]


# ── streaming ────────────────────────────────────────────────────────


async def test_streaming_in_scene_turn_carries_chips_in_the_done_frame() -> None:
    """The SSE ``done`` frame is ``response.model_dump()`` — one shape."""
    fixture = _Fixture()
    character_id = await fixture.character_id()
    await fixture.open_scene(character_id)

    stream, finalizer = await fixture.chat.send_message_stream(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
    )
    response = await finalizer.finish(await _drain(stream))

    assert "【劇情場景進行中】" in fixture.model.prompts[-1]
    assert response.model_dump(mode="json")["suggested_actions"] == [
        {"text": "靠過去"}, {"text": "先不說話"},
    ]


async def test_streaming_turn_outside_a_scene_sends_an_empty_chip_list() -> None:
    fixture = _Fixture()
    character_id = await fixture.character_id()

    stream, finalizer = await fixture.chat.send_message_stream(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )
    response = await finalizer.finish(await _drain(stream))

    assert fixture.chips.contexts == []
    assert response.model_dump(mode="json")["suggested_actions"] == []


async def test_streaming_in_scene_turn_stamps_the_idle_clock() -> None:
    fixture = _Fixture()
    character_id = await fixture.character_id()
    session = await fixture.open_scene(character_id)

    stream, finalizer = await fixture.chat.send_message_stream(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
    )
    await finalizer.finish(await _drain(stream))

    live = await fixture.sessions.get(session.id)
    assert live is not None
    assert live.last_activity_at > session.last_activity_at


# ── the opening's chips ──────────────────────────────────────────────


def _scene_service(
    *,
    sessions: InMemoryStorySceneSessionRepository,
    conversations: InMemoryConversationRepository,
    chips: StorySceneChipsWriterPort | None,
) -> StorySceneService:
    return StorySceneService(
        sessions=sessions,
        conversations=conversations,
        opener=_StubOpener(),
        material_providers=(_StubMaterialProvider(),),
        chips_writer=chips,
    )


async def _open_character(characters: InMemoryCharacterRepository):  # noqa: ANN201
    service = CharacterService(
        characters,
        conversation_repository=InMemoryConversationRepository(),
        memory_repository=InMemoryMemoryRepository(),
    )
    created = await service.create_character(
        CreateCharacterRequest(name="Aki", personality=["倔強"], interests=[]),
    )
    return await characters.get(created.id)


async def test_opening_offers_chips_for_a_player_with_no_opening_move() -> None:
    characters = InMemoryCharacterRepository()
    character = await _open_character(characters)
    chips = _RecordingChipsWriter(("問她在等誰", "什麼都不說，走過去"))
    service = _scene_service(
        sessions=InMemoryStorySceneSessionRepository(),
        conversations=InMemoryConversationRepository(),
        chips=chips,
    )

    opening = await service.open_scene(character, now=NOW)

    assert opening.suggested_actions == ("問她在等誰", "什麼都不說，走過去")
    # The chips see the curtain that just went up, not an empty scene.
    assert chips.contexts[0].recent_lines == (
        "風把話吹散了。", "Aki：你怎麼還在這裡？",
    )
    assert chips.contexts[0].session.id == opening.session.id


async def test_a_failing_chips_writer_does_not_fail_the_opening() -> None:
    """The player has already been shown the scene — and, hosted, paid."""
    characters = InMemoryCharacterRepository()
    character = await _open_character(characters)
    sessions = InMemoryStorySceneSessionRepository()
    service = _scene_service(
        sessions=sessions,
        conversations=InMemoryConversationRepository(),
        chips=_RecordingChipsWriter(raises=RuntimeError("chips down")),
    )

    opening = await service.open_scene(character, now=NOW)

    assert opening.suggested_actions == ()
    assert await sessions.get_open_for_character(character.id) is not None


async def test_opening_without_a_chips_writer_still_opens() -> None:
    characters = InMemoryCharacterRepository()
    character = await _open_character(characters)
    service = _scene_service(
        sessions=InMemoryStorySceneSessionRepository(),
        conversations=InMemoryConversationRepository(),
        chips=None,
    )

    opening = await service.open_scene(character, now=NOW)

    assert opening.suggested_actions == ()
    assert opening.narration.content == "風把話吹散了。"
