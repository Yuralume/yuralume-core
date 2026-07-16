"""聊天入口的 tenant lock 與獨立角色凍結語意。

- tenant state lock：由 ``SubscriptionAccessGuard`` 在任何 thaw 前擋住。
- legacy ``subscription_lapse``：保留 ``ChatSubscriptionFrozen`` fallback。
- ``idle`` 凍結（與舊資料 ``None``）：使用者一聊天即自動解凍（回歸）。
- ``manual`` 凍結：聊天不擋也不自動解凍（黏著，admin 主控台解凍）。
"""

from datetime import datetime, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import (
    ChatService,
    ChatSubscriptionFrozen,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
    SubscriptionAccessLocked,
)
from kokoro_link.domain.entities.character import (
    FREEZE_REASON_IDLE,
    FREEZE_REASON_MANUAL,
    FREEZE_REASON_SUBSCRIPTION_LAPSE,
)
from kokoro_link.infrastructure.llm.fake import FakeChatModel
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
from kokoro_link.infrastructure.repositories.in_memory_cloud_subscription import (
    InMemoryCloudSubscriptionRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

_FROZEN_AT = datetime(2026, 7, 1, tzinfo=timezone.utc)


def _build(subscription_access_guard=None):
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
        subscription_access_guard=subscription_access_guard,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    return chat_service, character_service, character_repository


async def _create(character_service, *, user_id="default"):
    return await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
        user_id=user_id,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_tenant_lock_blocks_chat_without_character_freeze(
    streaming: bool,
) -> None:
    operators = InMemoryOperatorProfileRepository()
    subscriptions = InMemoryCloudSubscriptionRepository()
    await operators.save(OperatorProfile(
        id="cloud-op",
        display_name="Player",
        cloud_account_id="acct-a",
        cloud_tenant_id="tenant-a",
        auth_provider="cloud",
    ))
    guard = SubscriptionAccessGuard(
        subscription_repository=subscriptions,
        operator_profile_repository=operators,
    )
    chat_service, character_service, repo = _build(guard)
    created = await _create(character_service, user_id="cloud-op")
    await subscriptions.set_locked("tenant-a", locked=True)

    method = (
        chat_service.send_message_stream
        if streaming
        else chat_service.send_message
    )
    with pytest.raises(SubscriptionAccessLocked):
        await method(
            SendChatMessageRequest(character_id=created.id, message="在嗎？"),
        )

    stored = await repo.get(created.id)
    assert stored.frozen is False
    assert stored.subscription_locked is False


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_tenant_lock_does_not_thaw_independent_idle_freeze(
    streaming: bool,
) -> None:
    operators = InMemoryOperatorProfileRepository()
    subscriptions = InMemoryCloudSubscriptionRepository()
    await operators.save(OperatorProfile(
        id="cloud-op",
        display_name="Player",
        cloud_account_id="acct-a",
        cloud_tenant_id="tenant-a",
        auth_provider="cloud",
    ))
    guard = SubscriptionAccessGuard(
        subscription_repository=subscriptions,
        operator_profile_repository=operators,
    )
    chat_service, character_service, repo = _build(guard)
    created = await _create(character_service, user_id="cloud-op")
    await repo.set_frozen(
        created.id,
        frozen=True,
        now=_FROZEN_AT,
        reason=FREEZE_REASON_IDLE,
    )
    await subscriptions.set_locked("tenant-a", locked=True)

    method = (
        chat_service.send_message_stream
        if streaming
        else chat_service.send_message
    )
    with pytest.raises(SubscriptionAccessLocked):
        await method(
            SendChatMessageRequest(character_id=created.id, message="在嗎？"),
        )

    stored = await repo.get(created.id)
    assert stored.frozen is True
    assert stored.frozen_at == _FROZEN_AT
    assert stored.frozen_reason == FREEZE_REASON_IDLE


@pytest.mark.asyncio
async def test_subscription_frozen_blocks_send_message() -> None:
    chat_service, character_service, repo = _build()
    created = await _create(character_service)
    await repo.set_frozen(
        created.id, frozen=True, now=_FROZEN_AT,
        reason=FREEZE_REASON_SUBSCRIPTION_LAPSE,
    )

    with pytest.raises(ChatSubscriptionFrozen):
        await chat_service.send_message(
            SendChatMessageRequest(character_id=created.id, message="在嗎？"),
        )

    # Still frozen — the chat turn never thawed it.
    stored = await repo.get(created.id)
    assert stored.frozen is True
    assert stored.frozen_reason == FREEZE_REASON_SUBSCRIPTION_LAPSE


@pytest.mark.asyncio
async def test_subscription_frozen_blocks_send_message_stream() -> None:
    chat_service, character_service, repo = _build()
    created = await _create(character_service)
    await repo.set_frozen(
        created.id, frozen=True, now=_FROZEN_AT,
        reason=FREEZE_REASON_SUBSCRIPTION_LAPSE,
    )

    with pytest.raises(ChatSubscriptionFrozen):
        await chat_service.send_message_stream(
            SendChatMessageRequest(character_id=created.id, message="在嗎？"),
        )

    assert (await repo.get(created.id)).frozen is True


@pytest.mark.asyncio
async def test_idle_freeze_still_thaws_on_chat() -> None:
    chat_service, character_service, repo = _build()
    created = await _create(character_service)
    await repo.set_frozen(
        created.id, frozen=True, now=_FROZEN_AT, reason=FREEZE_REASON_IDLE,
    )

    await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="嗨"),
    )
    await chat_service.wait_for_pending()

    thawed = await repo.get(created.id)
    assert thawed.frozen is False
    assert thawed.frozen_at is None
    assert thawed.frozen_reason is None


@pytest.mark.asyncio
async def test_manual_freeze_is_sticky_but_not_blocked() -> None:
    chat_service, character_service, repo = _build()
    created = await _create(character_service)
    await repo.set_frozen(
        created.id, frozen=True, now=_FROZEN_AT, reason=FREEZE_REASON_MANUAL,
    )

    # Chat is allowed (no raise) but the manual freeze persists.
    await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="嗨"),
    )
    await chat_service.wait_for_pending()

    stored = await repo.get(created.id)
    assert stored.frozen is True
    assert stored.frozen_reason == FREEZE_REASON_MANUAL
