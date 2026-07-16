import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.channel_binding_service import (
    ChannelBindingConflictError,
    ChannelBindingService,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.messaging_account_service import (
    MessagingAccountService,
)
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_channel_bindings import (
    InMemoryChannelBindingRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_messaging_accounts import (
    InMemoryMessagingAccountRepository,
)


async def _setup():
    char_repo = InMemoryCharacterRepository()
    conv_repo = InMemoryConversationRepository()
    mem_repo = InMemoryMemoryRepository()
    acct_repo = InMemoryMessagingAccountRepository()
    bind_repo = InMemoryChannelBindingRepository()
    character_service = CharacterService(
        char_repo,
        conversation_repository=conv_repo,
        memory_repository=mem_repo,
    )
    account_service = MessagingAccountService(
        account_repository=acct_repo,
        binding_repository=bind_repo,
        character_repository=char_repo,
    )
    binding_service = ChannelBindingService(
        binding_repository=bind_repo,
        account_repository=acct_repo,
    )
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )
    account = await account_service.create(
        character_id=character.id,
        platform=Platform.TELEGRAM,
        credentials={"bot_token": "t"},
    )
    return binding_service, account


@pytest.mark.asyncio
async def test_create_binding_for_account() -> None:
    binding_service, account = await _setup()

    binding = await binding_service.create(
        account_id=account.id, chat_ref="chat-42",
    )

    assert binding.account_id == account.id
    assert binding.chat_ref == "chat-42"
    assert binding.enabled is True


@pytest.mark.asyncio
async def test_create_rejects_unknown_account() -> None:
    binding_service, _ = await _setup()
    with pytest.raises(ValueError):
        await binding_service.create(account_id="ghost", chat_ref="c1")


@pytest.mark.asyncio
async def test_duplicate_chat_under_same_account_conflicts() -> None:
    binding_service, account = await _setup()

    await binding_service.create(account_id=account.id, chat_ref="chat-1")
    with pytest.raises(ChannelBindingConflictError):
        await binding_service.create(account_id=account.id, chat_ref="chat-1")


@pytest.mark.asyncio
async def test_set_enabled_and_delete() -> None:
    binding_service, account = await _setup()
    binding = await binding_service.create(
        account_id=account.id, chat_ref="chat-1",
    )

    disabled = await binding_service.set_enabled(binding.id, enabled=False)
    assert disabled.enabled is False

    assert await binding_service.delete(binding.id) is True
    assert await binding_service.list_for_account(account.id) == []
