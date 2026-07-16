import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.channel_binding_service import ChannelBindingService
from kokoro_link.application.services.messaging_account_service import (
    MessagingAccountConflictError,
    MessagingAccountService,
)
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
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


_TG_CREDS = {"bot_token": "t", "webhook_secret": "s"}
_LINE_CREDS = {"channel_secret": "cs", "channel_access_token": "ca"}
_DISCORD_CREDS = {"bot_token": "discord-token"}
_WHATSAPP_CREDS = {
    "sidecar_url": "http://127.0.0.1:32190/",
    "session_id": "mio",
}


async def _setup(
    *,
    default_whatsapp_api_token: str = "",
) -> tuple[MessagingAccountService, ChannelBindingService, CharacterService]:
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
        default_whatsapp_api_token=default_whatsapp_api_token,
    )
    binding_service = ChannelBindingService(
        binding_repository=bind_repo,
        account_repository=acct_repo,
    )
    return account_service, binding_service, character_service


@pytest.mark.asyncio
async def test_create_account_for_character() -> None:
    account_service, _, character_service = await _setup()
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )

    account = await account_service.create(
        character_id=character.id,
        platform=Platform.TELEGRAM,
        credentials=_TG_CREDS,
        display_name="Mio's TG bot",
    )

    assert account.platform == Platform.TELEGRAM
    assert account.character_id == character.id
    assert account.credentials == _TG_CREDS
    assert account.display_name == "Mio's TG bot"
    assert account.webhook_slug
    assert account.delivery_mode == DeliveryMode.POLLING


@pytest.mark.asyncio
async def test_create_rejects_unknown_character() -> None:
    account_service, _, _ = await _setup()
    with pytest.raises(ValueError):
        await account_service.create(
            character_id="ghost",
            platform=Platform.TELEGRAM,
            credentials=_TG_CREDS,
        )


@pytest.mark.asyncio
async def test_second_account_on_same_platform_conflicts() -> None:
    account_service, _, character_service = await _setup()
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )
    await account_service.create(
        character_id=character.id,
        platform=Platform.TELEGRAM,
        credentials=_TG_CREDS,
    )
    with pytest.raises(MessagingAccountConflictError):
        await account_service.create(
            character_id=character.id,
            platform=Platform.TELEGRAM,
            credentials={"bot_token": "other"},
        )


@pytest.mark.asyncio
async def test_telegram_bot_token_cannot_be_bound_to_multiple_accounts() -> None:
    account_service, _, character_service = await _setup()
    first = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )
    second = await character_service.create_character(
        CreateCharacterRequest(name="Rin", personality=[], interests=[]),
    )
    await account_service.create(
        character_id=first.id,
        platform=Platform.TELEGRAM,
        credentials={"bot_token": "shared"},
    )

    with pytest.raises(MessagingAccountConflictError):
        await account_service.create(
            character_id=second.id,
            platform=Platform.TELEGRAM,
            credentials={"bot_token": "shared"},
        )


@pytest.mark.asyncio
async def test_character_can_have_one_account_per_platform() -> None:
    account_service, _, character_service = await _setup()
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )

    tg = await account_service.create(
        character_id=character.id,
        platform=Platform.TELEGRAM,
        credentials=_TG_CREDS,
    )
    line = await account_service.create(
        character_id=character.id,
        platform=Platform.LINE,
        credentials=_LINE_CREDS,
    )
    discord = await account_service.create(
        character_id=character.id,
        platform=Platform.DISCORD,
        credentials=_DISCORD_CREDS,
    )
    whatsapp = await account_service.create(
        character_id=character.id,
        platform=Platform.WHATSAPP,
        credentials=_WHATSAPP_CREDS,
    )

    listed = await account_service.list_for_character(character.id)
    assert {a.id for a in listed} == {tg.id, line.id, discord.id, whatsapp.id}


@pytest.mark.asyncio
async def test_discord_account_defaults_to_gateway_delivery_mode() -> None:
    account_service, _, character_service = await _setup()
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )

    account = await account_service.create(
        character_id=character.id,
        platform=Platform.DISCORD,
        credentials=_DISCORD_CREDS,
    )

    assert account.delivery_mode == DeliveryMode.GATEWAY


@pytest.mark.asyncio
async def test_discord_account_rejects_non_gateway_delivery_mode() -> None:
    account_service, _, character_service = await _setup()
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )

    with pytest.raises(ValueError):
        await account_service.create(
            character_id=character.id,
            platform=Platform.DISCORD,
            credentials=_DISCORD_CREDS,
            delivery_mode=DeliveryMode.WEBHOOK,
        )


@pytest.mark.asyncio
async def test_discord_bot_token_cannot_be_bound_to_multiple_accounts() -> None:
    account_service, _, character_service = await _setup()
    first = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )
    second = await character_service.create_character(
        CreateCharacterRequest(name="Rin", personality=[], interests=[]),
    )
    await account_service.create(
        character_id=first.id,
        platform=Platform.DISCORD,
        credentials={"bot_token": "shared-discord"},
    )

    with pytest.raises(MessagingAccountConflictError):
        await account_service.create(
            character_id=second.id,
            platform=Platform.DISCORD,
            credentials={"bot_token": "shared-discord"},
        )


@pytest.mark.asyncio
async def test_whatsapp_account_defaults_to_gateway_delivery_mode() -> None:
    account_service, _, character_service = await _setup()
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )

    account = await account_service.create(
        character_id=character.id,
        platform=Platform.WHATSAPP,
        credentials=_WHATSAPP_CREDS,
    )

    assert account.delivery_mode == DeliveryMode.GATEWAY


@pytest.mark.asyncio
async def test_whatsapp_account_defaults_to_container_sidecar_credentials() -> None:
    account_service, _, character_service = await _setup()
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )

    account = await account_service.create(
        character_id=character.id,
        platform=Platform.WHATSAPP,
        credentials={},
    )

    assert account.credentials == {
        "sidecar_url": "http://whatsapp-sidecar:32190",
        "session_id": f"character-{character.id}",
    }
    assert account.delivery_mode == DeliveryMode.GATEWAY


@pytest.mark.asyncio
async def test_whatsapp_account_uses_default_sidecar_api_token() -> None:
    account_service, _, character_service = await _setup(
        default_whatsapp_api_token="sidecar-secret",
    )
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )

    account = await account_service.create(
        character_id=character.id,
        platform=Platform.WHATSAPP,
        credentials={},
    )

    assert account.credentials["api_token"] == "sidecar-secret"


@pytest.mark.asyncio
async def test_whatsapp_account_rejects_non_gateway_delivery_mode() -> None:
    account_service, _, character_service = await _setup()
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )

    with pytest.raises(ValueError):
        await account_service.create(
            character_id=character.id,
            platform=Platform.WHATSAPP,
            credentials=_WHATSAPP_CREDS,
            delivery_mode=DeliveryMode.WEBHOOK,
        )


@pytest.mark.asyncio
async def test_whatsapp_sidecar_session_cannot_be_bound_to_multiple_accounts() -> None:
    account_service, _, character_service = await _setup()
    first = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )
    second = await character_service.create_character(
        CreateCharacterRequest(name="Rin", personality=[], interests=[]),
    )
    await account_service.create(
        character_id=first.id,
        platform=Platform.WHATSAPP,
        credentials=_WHATSAPP_CREDS,
    )

    with pytest.raises(MessagingAccountConflictError):
        await account_service.create(
            character_id=second.id,
            platform=Platform.WHATSAPP,
            credentials={
                "sidecar_url": "http://127.0.0.1:32190",
                "session_id": "mio",
            },
        )


@pytest.mark.asyncio
async def test_update_credentials_and_allowlist() -> None:
    account_service, _, character_service = await _setup()
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )
    account = await account_service.create(
        character_id=character.id,
        platform=Platform.TELEGRAM,
        credentials=_TG_CREDS,
    )

    updated = await account_service.update(
        account.id,
        credentials={"bot_token": "rotated"},
        allowed_sender_refs=("U1", "U2"),
        display_name="Rotated",
    )

    assert updated.credentials == {"bot_token": "rotated"}
    assert updated.allowed_sender_refs == ("U1", "U2")
    assert updated.display_name == "Rotated"


@pytest.mark.asyncio
async def test_update_rejects_telegram_bot_token_bound_to_another_account() -> None:
    account_service, _, character_service = await _setup()
    first = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )
    second = await character_service.create_character(
        CreateCharacterRequest(name="Rin", personality=[], interests=[]),
    )
    await account_service.create(
        character_id=first.id,
        platform=Platform.TELEGRAM,
        credentials={"bot_token": "taken"},
    )
    account = await account_service.create(
        character_id=second.id,
        platform=Platform.TELEGRAM,
        credentials={"bot_token": "original"},
    )

    with pytest.raises(MessagingAccountConflictError):
        await account_service.update(
            account.id,
            credentials={"bot_token": "taken"},
        )


@pytest.mark.asyncio
async def test_update_delivery_mode_to_webhook() -> None:
    account_service, _, character_service = await _setup()
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )
    account = await account_service.create(
        character_id=character.id,
        platform=Platform.TELEGRAM,
        credentials=_TG_CREDS,
    )

    updated = await account_service.update(
        account.id, delivery_mode=DeliveryMode.WEBHOOK,
    )

    assert updated.delivery_mode == DeliveryMode.WEBHOOK


@pytest.mark.asyncio
async def test_line_account_rejects_polling_delivery_mode() -> None:
    account_service, _, character_service = await _setup()
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )

    with pytest.raises(ValueError):
        await account_service.create(
            character_id=character.id,
            platform=Platform.LINE,
            credentials=_LINE_CREDS,
            delivery_mode=DeliveryMode.POLLING,
        )


@pytest.mark.asyncio
async def test_update_missing_account_raises() -> None:
    account_service, _, _ = await _setup()
    with pytest.raises(ValueError):
        await account_service.update("missing", enabled=False)


@pytest.mark.asyncio
async def test_delete_removes_account_and_its_bindings() -> None:
    account_service, binding_service, character_service = await _setup()
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )
    account = await account_service.create(
        character_id=character.id,
        platform=Platform.TELEGRAM,
        credentials=_TG_CREDS,
    )
    await binding_service.create(account_id=account.id, chat_ref="c1")
    await binding_service.create(account_id=account.id, chat_ref="c2")

    assert await account_service.delete(account.id) is True
    assert await account_service.get(account.id) is None
    assert await binding_service.list_for_account(account.id) == []


@pytest.mark.asyncio
async def test_find_by_slug() -> None:
    account_service, _, character_service = await _setup()
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=[], interests=[]),
    )
    account = await account_service.create(
        character_id=character.id,
        platform=Platform.TELEGRAM,
        credentials=_TG_CREDS,
    )

    assert await account_service.find_by_slug(account.webhook_slug) == account
    assert await account_service.find_by_slug("missing") is None
