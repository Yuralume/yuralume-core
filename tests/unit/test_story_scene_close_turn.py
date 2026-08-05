"""The in-scene turn that ends the scene (SC1-D, chat wiring).

SC2 reported the contract gap this file closes: a scene that resolves on
turn N has to come back *on turn N's reply*, or the player keeps typing
into a scene that is already over and only finds out on reload. So the
reply grows two nullable fields — the closed session and the wrap-up
narration — and everything here is about when they are and are not there.

The other half is that they cost an ordinary chat turn nothing. A turn
outside a scene never asks; a turn inside an unfinished scene comes back
byte-identical to the pre-SC1-D shape apart from two nulls; and a verdict
that crashes is invisible to the player.
"""

from __future__ import annotations

from datetime import datetime, timezone

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
    StorySceneClosingDraft,
    StorySceneCloserPort,
    StorySceneOpenerPort,
)
from kokoro_link.domain.entities.conversation import (
    SOURCE_WEB,
    Conversation,
    MessageKind,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_RESOLVED,
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

CLOSING_NARRATION = "話說到這裡就停了，她低頭把琴收好。"
CANON_SUMMARY = "我們把那句話說完了。"


# ── doubles ──────────────────────────────────────────────────────────


class _StubCloser(StorySceneCloserPort):
    def __init__(
        self,
        draft: StorySceneClosingDraft | None,
        *,
        raises: Exception | None = None,
    ) -> None:
        self._draft = draft
        self._raises = raises
        self.calls = 0

    async def write_closing(self, context):  # noqa: ANN001
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        return self._draft


class _RecordingChipsWriter(StorySceneChipsWriterPort):
    def __init__(self) -> None:
        self.contexts: list[StorySceneChipsContext] = []

    async def suggest_actions(self, context):  # noqa: ANN001
        self.contexts.append(context)
        return ("靠過去", "先不說話")


class _UnusedOpener(StorySceneOpenerPort):
    async def write_opening(self, context):  # noqa: ANN001
        raise AssertionError("these turns never open a scene")


_RESOLVED = StorySceneClosingDraft(
    resolved=True,
    closing_narration=CLOSING_NARRATION,
    canon_summary=CANON_SUMMARY,
)
_UNRESOLVED = StorySceneClosingDraft(resolved=False)


# ── fixture ──────────────────────────────────────────────────────────


class _Fixture:
    def __init__(
        self,
        *,
        closer: _StubCloser | None = None,
        wire_closer: bool = True,
    ) -> None:
        self.characters = InMemoryCharacterRepository()
        self.conversations = InMemoryConversationRepository()
        self.memories = InMemoryMemoryRepository()
        self.sessions = InMemoryStorySceneSessionRepository()
        self.chips = _RecordingChipsWriter()
        self.closer = closer or _StubCloser(_UNRESOLVED)
        registry = InMemoryChatModelRegistry(default_provider_id="fake")
        registry.register(FakeChatModel("fake"))
        self.scenes = StorySceneService(
            sessions=self.sessions,
            conversations=self.conversations,
            opener=_UnusedOpener(),
            material_providers=(),
            closer=self.closer,
            memory_repository=self.memories,
            local_tz=timezone.utc,
        )
        self.chat = ChatService(
            character_repository=self.characters,
            conversation_repository=self.conversations,
            memory_repository=self.memories,
            post_turn_processor=NullPostTurnProcessor(),
            prompt_context_builder=DefaultPromptContextBuilder(),
            model_registry=registry,
            state_engine=SimpleStateEngine(),
            story_scene_sessions=self.sessions,
            story_scene_chips_writer=self.chips,
            story_scene_service=self.scenes if wire_closer else None,
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
        # Pinned to the character's real web thread: the wrap-up lands in
        # the conversation the session names, not the one the caller holds.
        conversation = Conversation.start(
            character_id=character_id, source=SOURCE_WEB,
        )
        await self.conversations.save(conversation)
        session = StorySceneSession.open_scene(
            character_id=character_id,
            conversation_id=conversation.id,
            source_layer=SCENE_LAYER_SIDE_STORY,
            title="把話說完",
            location="頂樓天台",
            mood="欲言又止",
            dramatic_question="她要承認自己練得不夠嗎？",
            opened_at=NOW,
        )
        await self.sessions.add(session)
        return session


async def _drain(stream) -> str:  # noqa: ANN001
    return "".join([token async for token in stream])


# ── a turn that ends the scene ───────────────────────────────────────


async def test_a_resolved_turn_returns_the_closed_scene_and_the_wrap_up(
) -> None:
    fixture = _Fixture(closer=_StubCloser(_RESOLVED))
    character_id = await fixture.character_id()
    session = await fixture.open_scene(character_id)

    reply = await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我說完了。"),
    )

    assert reply.story_scene_session is not None
    assert reply.story_scene_session.id == session.id
    assert reply.story_scene_session.status == "closed"
    assert reply.story_scene_session.closed_reason == SCENE_CLOSE_RESOLVED
    assert reply.story_scene_closing is not None
    assert reply.story_scene_closing.content == CLOSING_NARRATION
    assert reply.story_scene_closing.kind == MessageKind.SCENE_NARRATION.value


async def test_a_resolved_turn_offers_no_next_move() -> None:
    """The scene is over; suggesting what to do next in it would be a
    chip that leads nowhere."""
    fixture = _Fixture(closer=_StubCloser(_RESOLVED))
    character_id = await fixture.character_id()
    await fixture.open_scene(character_id)

    reply = await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我說完了。"),
    )

    assert reply.suggested_actions == []


async def test_a_resolved_turn_actually_closes_and_lands_the_scene() -> None:
    fixture = _Fixture(closer=_StubCloser(_RESOLVED))
    character_id = await fixture.character_id()
    session = await fixture.open_scene(character_id)

    await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我說完了。"),
    )

    assert await fixture.sessions.get_open_for_character(character_id) is None
    stored = await fixture.conversations.get(session.conversation_id)
    assert stored.messages[-1].content == CLOSING_NARRATION
    memories = await fixture.memories.query(character_id)
    assert any(m.content == CANON_SUMMARY for m in memories)


# ── every other turn ─────────────────────────────────────────────────


async def test_an_unresolved_turn_carries_no_scene_fields() -> None:
    fixture = _Fixture(closer=_StubCloser(_UNRESOLVED))
    character_id = await fixture.character_id()
    await fixture.open_scene(character_id)

    reply = await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
    )

    assert reply.story_scene_session is None
    assert reply.story_scene_closing is None
    # and the scene keeps playing, chips and all
    assert [chip.text for chip in reply.suggested_actions] == [
        "靠過去", "先不說話",
    ]
    assert await fixture.sessions.get_open_for_character(character_id)


async def test_a_turn_outside_a_scene_never_asks_for_a_verdict() -> None:
    """The feature must cost a plain chat turn exactly nothing."""
    fixture = _Fixture(closer=_StubCloser(_RESOLVED))
    character_id = await fixture.character_id()

    reply = await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="哈囉"),
    )

    assert fixture.closer.calls == 0
    assert reply.story_scene_session is None
    assert reply.story_scene_closing is None


async def test_a_failed_verdict_is_invisible_to_the_player() -> None:
    fixture = _Fixture(
        closer=_StubCloser(None, raises=RuntimeError("upstream down")),
    )
    character_id = await fixture.character_id()
    await fixture.open_scene(character_id)

    reply = await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
    )

    assert reply.assistant_message is not None
    assert reply.story_scene_session is None
    assert await fixture.sessions.get_open_for_character(character_id)


async def test_an_unwired_wrap_up_is_the_pre_sc1d_chat_path() -> None:
    fixture = _Fixture(closer=_StubCloser(_RESOLVED), wire_closer=False)
    character_id = await fixture.character_id()
    await fixture.open_scene(character_id)

    reply = await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
    )

    assert fixture.closer.calls == 0
    assert reply.story_scene_session is None
    assert [chip.text for chip in reply.suggested_actions] == [
        "靠過去", "先不說話",
    ]


async def test_a_scene_closed_mid_turn_is_not_closed_again() -> None:
    """The activity stamp already answered "is this scene live?"; asking
    for a verdict on a scene nobody is in would be a wasted model call."""
    fixture = _Fixture(closer=_StubCloser(_RESOLVED))
    character_id = await fixture.character_id()
    session = await fixture.open_scene(character_id)
    await fixture.sessions.close(
        session.id, reason=SCENE_CLOSE_RESOLVED, at=NOW,
    )

    reply = await fixture.chat.send_message(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
    )

    assert fixture.closer.calls == 0
    assert reply.story_scene_session is None


# ── streaming ────────────────────────────────────────────────────────


async def test_the_done_frame_carries_the_wrap_up_in_the_same_shape() -> None:
    """The SSE ``done`` frame is ``response.model_dump()``, so the two
    transports must not grow two shapes."""
    fixture = _Fixture(closer=_StubCloser(_RESOLVED))
    character_id = await fixture.character_id()
    await fixture.open_scene(character_id)

    stream, finalizer = await fixture.chat.send_message_stream(
        SendChatMessageRequest(character_id=character_id, message="我說完了。"),
    )
    response = await finalizer.finish(await _drain(stream))

    body = response.model_dump(mode="json")
    assert body["story_scene_session"]["status"] == "closed"
    assert body["story_scene_closing"]["content"] == CLOSING_NARRATION
    assert body["suggested_actions"] == []


async def test_the_done_frame_of_an_unresolved_turn_carries_nulls() -> None:
    fixture = _Fixture(closer=_StubCloser(_UNRESOLVED))
    character_id = await fixture.character_id()
    await fixture.open_scene(character_id)

    stream, finalizer = await fixture.chat.send_message_stream(
        SendChatMessageRequest(character_id=character_id, message="我在。"),
    )
    response = await finalizer.finish(await _drain(stream))

    body = response.model_dump(mode="json")
    assert body["story_scene_session"] is None
    assert body["story_scene_closing"] is None
