"""聊天入口的 tenant lock 與獨立角色凍結語意。

- tenant state lock：由 ``SubscriptionAccessGuard`` 在任何 thaw 前擋住。
- legacy ``subscription_lapse``：保留 ``ChatSubscriptionFrozen`` fallback。
- ``idle`` 凍結（與舊資料 ``None``）：使用者一聊天即自動解凍（回歸）。
- ``manual`` 凍結：聊天不擋也不自動解凍（黏著，admin 主控台解凍）。
- ``restore`` 凍結（CB A3）：備份還原落地中，聊天硬擋、不自動解凍；
  還原成功才解凍、失敗清理直接刪列。
- ``exclusive_contract_end`` 凍結（D7／EC10）：IP 合約終止，拍板語意是
  「資料保留、不可互動」，聊天硬擋、不自動解凍；只有同一條 per-card
  訊息的 unfreeze 能解，解完聊天即恢復。
"""

from datetime import datetime, timezone

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import (
    ChatCharacterContractEndedError,
    ChatCharacterRestoringError,
    ChatService,
    ChatSubscriptionFrozen,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
    SubscriptionAccessLocked,
)
from kokoro_link.domain.entities.character import (
    FREEZE_REASON_EXCLUSIVE_CONTRACT_END,
    FREEZE_REASON_IDLE,
    FREEZE_REASON_MANUAL,
    FREEZE_REASON_RESTORE,
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
@pytest.mark.parametrize("streaming", [False, True])
async def test_restore_freeze_blocks_chat_and_never_thaws(
    streaming: bool,
) -> None:
    """CB A3 — a mid-restore character (frozen_reason='restore') is only
    half there: memories / media are still landing, and any turn accepted
    now would be destroyed by the restore's failure cleanup. Chat must
    refuse with the typed error and must NOT auto-thaw."""
    chat_service, character_service, repo = _build()
    created = await _create(character_service)
    await repo.set_frozen(
        created.id, frozen=True, now=_FROZEN_AT,
        reason=FREEZE_REASON_RESTORE,
    )

    method = (
        chat_service.send_message_stream
        if streaming
        else chat_service.send_message
    )
    with pytest.raises(ChatCharacterRestoringError):
        await method(
            SendChatMessageRequest(character_id=created.id, message="在嗎？"),
        )

    stored = await repo.get(created.id)
    assert stored.frozen is True
    assert stored.frozen_reason == FREEZE_REASON_RESTORE


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


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_contract_end_freeze_blocks_chat_and_never_thaws(
    streaming: bool,
) -> None:
    """D7 / EC10 — a card's IP contract ending means "資料保留、不可互動".

    A sticky-but-chattable freeze (the ``manual`` shape) would leave the
    player talking to a character the licensor has withdrawn, which is the
    one outcome the contract-termination switch exists to prevent. The
    error is its own type: borrowing ``ChatSubscriptionFrozen`` would tell
    the player to renew a subscription that is not the problem."""
    chat_service, character_service, repo = _build()
    created = await _create(character_service)
    await repo.set_frozen(
        created.id, frozen=True, now=_FROZEN_AT,
        reason=FREEZE_REASON_EXCLUSIVE_CONTRACT_END,
    )

    method = (
        chat_service.send_message_stream
        if streaming
        else chat_service.send_message
    )
    with pytest.raises(ChatCharacterContractEndedError):
        await method(
            SendChatMessageRequest(character_id=created.id, message="在嗎？"),
        )

    stored = await repo.get(created.id)
    assert stored.frozen is True
    assert stored.frozen_reason == FREEZE_REASON_EXCLUSIVE_CONTRACT_END


@pytest.mark.asyncio
async def test_contract_end_unfreeze_restores_chat() -> None:
    """The block is exactly as reversible as the freeze: the per-card
    ``unfreeze`` message clears the row and the next turn goes through."""
    chat_service, character_service, repo = _build()
    created = await _create(character_service)
    await repo.set_frozen(
        created.id, frozen=True, now=_FROZEN_AT,
        reason=FREEZE_REASON_EXCLUSIVE_CONTRACT_END,
    )
    await repo.set_frozen(created.id, frozen=False, now=_FROZEN_AT)

    await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="回來了"),
    )
    await chat_service.wait_for_pending()

    stored = await repo.get(created.id)
    assert stored.frozen is False
    assert stored.frozen_reason is None


@pytest.mark.asyncio
async def test_contract_end_error_is_not_a_subscription_error() -> None:
    """釘死型別分離：兩個凍結來源的錯誤不可互相被 except 攔到，
    否則玩家會收到「續訂即可」這種與事實不符的指引。"""
    assert not issubclass(
        ChatCharacterContractEndedError, ChatSubscriptionFrozen,
    )
    assert not issubclass(
        ChatSubscriptionFrozen, ChatCharacterContractEndedError,
    )
