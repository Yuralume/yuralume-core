"""使用者互動即自動解凍（CHARACTER_FREEZE_PLAN）。

凍結只停背景活動；只要使用者送訊息跟角色互動，就必須立即自動解凍，
讓背景 scheduler 恢復對該角色的排程。
"""

from datetime import datetime, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_characters import InMemoryCharacterRepository
from kokoro_link.infrastructure.repositories.in_memory_conversations import InMemoryConversationRepository
from kokoro_link.infrastructure.state.simple import SimpleStateEngine


def _build_chat_service() -> tuple[ChatService, CharacterService, InMemoryCharacterRepository]:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))

    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    return chat_service, character_service, character_repository


@pytest.mark.asyncio
async def test_send_message_unfreezes_frozen_character() -> None:
    chat_service, character_service, character_repository = _build_chat_service()
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    frozen_at = datetime(2026, 7, 1, tzinfo=timezone.utc)
    await character_repository.set_frozen(created.id, frozen=True, now=frozen_at)
    frozen_character = await character_repository.get(created.id)
    assert frozen_character is not None
    assert frozen_character.frozen is True
    assert frozen_character.frozen_at == frozen_at

    await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="嗨，好久不見"),
    )
    await chat_service.wait_for_pending()

    unfrozen_character = await character_repository.get(created.id)
    assert unfrozen_character is not None
    assert unfrozen_character.frozen is False
    assert unfrozen_character.frozen_at is None


@pytest.mark.asyncio
async def test_send_message_stream_unfreezes_frozen_character() -> None:
    chat_service, character_service, character_repository = _build_chat_service()
    created = await character_service.create_character(
        CreateCharacterRequest(name="Rin", personality=["curious"], interests=[]),
    )

    await character_repository.set_frozen(
        created.id, frozen=True, now=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    token_stream, finalizer = await chat_service.send_message_stream(
        SendChatMessageRequest(character_id=created.id, message="在嗎？"),
    )
    full_text = ""
    async for token in token_stream:
        full_text += token
    await finalizer.finish(full_text)
    await chat_service.wait_for_pending()

    unfrozen_character = await character_repository.get(created.id)
    assert unfrozen_character is not None
    assert unfrozen_character.frozen is False
    assert unfrozen_character.frozen_at is None
