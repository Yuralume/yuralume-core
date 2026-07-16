"""Shared wiring helper for messaging-related unit tests.

Kept as an underscore-prefixed module so pytest ignores it during
collection but test files can still import it via
``from tests.unit._messaging_harness import build_messaging_harness``.
"""

import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.messaging import router as messaging_router
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.channel_binding_service import (
    ChannelBindingService,
)
from kokoro_link.application.services.album_service import AlbumService
from kokoro_link.application.services.character_draft_service import (
    CharacterDraftService,
)
from kokoro_link.application.services.companion_draft_service import (
    CompanionDraftService,
)
from kokoro_link.application.services.character_image_service import (
    CharacterImageService,
)
from kokoro_link.application.services.character_lora_service import (
    CharacterLoraService,
)
from kokoro_link.application.services.active_llm_provider import (
    PreferenceBackedActiveLLMProvider,
)
from kokoro_link.application.services.character_encounter_service import (
    CharacterEncounterMemoryWriter,
    CharacterEncounterPlanner,
    CharacterEncounterRunner,
    CharacterEncounterService,
)
from kokoro_link.application.services.character_relationship_service import (
    CharacterRelationshipService,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.goal_service import GoalService
from kokoro_link.application.services.memory_admin_service import (
    MemoryAdminService,
)
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_album import (
    InMemoryAlbumRepository,
)
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.repositories.in_memory_tool_invocations import (
    InMemoryToolInvocationRepository,
)
from kokoro_link.infrastructure.tools.fake_tools import EchoTool, FakeImageTool
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry
from kokoro_link.application.services.memory_consolidation_service import (
    MemoryConsolidationService,
)
from kokoro_link.application.services.messaging_account_service import (
    MessagingAccountService,
)
from kokoro_link.application.services.messaging_dispatcher import MessagingDispatcher
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.bootstrap.container import ServiceContainer
from kokoro_link.contracts.messaging import InboundMessage
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.character_draft.stub import (
    StubCharacterDraftGenerator,
    StubCompanionDraftGenerator,
)
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.embedder.null import NullEmbedder
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.memory.null_consolidator import NullMemoryConsolidator
from kokoro_link.infrastructure.messaging.debounce import InboundDebouncer
from kokoro_link.infrastructure.messaging.fake_adapter import FakeChannelAdapter
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_channel_bindings import (
    InMemoryChannelBindingRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_character_encounters import (
    InMemoryCharacterEncounterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_character_relationships import (
    InMemoryCharacterRelationshipRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_goals import InMemoryGoalRepository
from kokoro_link.infrastructure.repositories.in_memory_messaging_accounts import (
    InMemoryMessagingAccountRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
    InMemoryStorySeedRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_state_history import (
    InMemoryStateHistoryRepository,
)
from kokoro_link.infrastructure.schedule.null_planner import NullSchedulePlanner
from kokoro_link.infrastructure.state.simple import SimpleStateEngine


@dataclass
class MessagingHarness:
    character_repository: InMemoryCharacterRepository
    conversation_repository: InMemoryConversationRepository
    memory_repository: InMemoryMemoryRepository
    model_registry: InMemoryChatModelRegistry
    preferences_repository: InMemoryPreferencesRepository
    account_repository: InMemoryMessagingAccountRepository
    binding_repository: InMemoryChannelBindingRepository
    character_service: CharacterService
    account_service: MessagingAccountService
    binding_service: ChannelBindingService
    chat_service: ChatService
    dispatcher: MessagingDispatcher
    telegram_adapter: FakeChannelAdapter
    line_adapter: FakeChannelAdapter
    discord_adapter: FakeChannelAdapter
    whatsapp_adapter: FakeChannelAdapter


def build_messaging_harness(
    *,
    public_base_url: str = "",
    public_base_url_provider: Callable[[], Awaitable[str]] | None = None,
    operator_language_resolver: (
        Callable[[str], Awaitable[str]] | None
    ) = None,
) -> MessagingHarness:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    account_repository = InMemoryMessagingAccountRepository()
    binding_repository = InMemoryChannelBindingRepository()
    preferences_repository = InMemoryPreferencesRepository()

    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    active_llm_provider = PreferenceBackedActiveLLMProvider(
        registry=registry,
        preferences=preferences_repository,
        default_provider_id="fake",
    )
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        active_llm_provider=active_llm_provider,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    account_service = MessagingAccountService(
        account_repository=account_repository,
        binding_repository=binding_repository,
        character_repository=character_repository,
    )
    binding_service = ChannelBindingService(
        binding_repository=binding_repository,
        account_repository=account_repository,
    )
    telegram_adapter = FakeChannelAdapter(Platform.TELEGRAM)
    line_adapter = FakeChannelAdapter(Platform.LINE)
    discord_adapter = FakeChannelAdapter(Platform.DISCORD)
    whatsapp_adapter = FakeChannelAdapter(Platform.WHATSAPP)
    dispatcher = MessagingDispatcher(
        account_repository=account_repository,
        binding_repository=binding_repository,
        conversation_repository=conversation_repository,
        chat_service=chat_service,
        adapters={
            Platform.TELEGRAM: telegram_adapter,
            Platform.LINE: line_adapter,
            Platform.DISCORD: discord_adapter,
            Platform.WHATSAPP: whatsapp_adapter,
        },
        debouncer=InboundDebouncer(ttl_seconds=60.0),
        public_base_url=public_base_url,
        public_base_url_provider=public_base_url_provider,
        operator_language_resolver=operator_language_resolver,
    )
    return MessagingHarness(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        model_registry=registry,
        preferences_repository=preferences_repository,
        account_repository=account_repository,
        binding_repository=binding_repository,
        character_service=character_service,
        account_service=account_service,
        binding_service=binding_service,
        chat_service=chat_service,
        dispatcher=dispatcher,
        telegram_adapter=telegram_adapter,
        line_adapter=line_adapter,
        discord_adapter=discord_adapter,
        whatsapp_adapter=whatsapp_adapter,
    )


async def create_character(
    harness: MessagingHarness,
    name: str = "Mio",
    proactive_enabled: bool | None = None,
) -> Character:
    payload = CreateCharacterRequest(name=name, personality=[], interests=[])
    if proactive_enabled is not None:
        payload.proactive_enabled = proactive_enabled
    return await harness.character_service.create_character(
        payload,
    )


async def create_telegram_account(
    harness: MessagingHarness,
    *,
    character_id: str,
    bot_token: str = "TG-TOKEN",
    webhook_secret: str = "",
    allowed_sender_refs: tuple[str, ...] = (),
    delivery_mode: DeliveryMode | None = DeliveryMode.WEBHOOK,
) -> MessagingAccount:
    credentials = {"bot_token": bot_token}
    if webhook_secret:
        credentials["webhook_secret"] = webhook_secret
    return await harness.account_service.create(
        character_id=character_id,
        platform=Platform.TELEGRAM,
        credentials=credentials,
        allowed_sender_refs=allowed_sender_refs,
        delivery_mode=delivery_mode,
    )


async def create_line_account(
    harness: MessagingHarness,
    *,
    character_id: str,
    channel_secret: str = "SEC",
    channel_access_token: str = "AT",
    allowed_sender_refs: tuple[str, ...] = (),
) -> MessagingAccount:
    return await harness.account_service.create(
        character_id=character_id,
        platform=Platform.LINE,
        credentials={
            "channel_secret": channel_secret,
            "channel_access_token": channel_access_token,
        },
        allowed_sender_refs=allowed_sender_refs,
    )


async def create_discord_account(
    harness: MessagingHarness,
    *,
    character_id: str,
    bot_token: str = "DISCORD-TOKEN",
    allowed_sender_refs: tuple[str, ...] = (),
) -> MessagingAccount:
    return await harness.account_service.create(
        character_id=character_id,
        platform=Platform.DISCORD,
        credentials={"bot_token": bot_token},
        allowed_sender_refs=allowed_sender_refs,
    )


async def create_whatsapp_account(
    harness: MessagingHarness,
    *,
    character_id: str,
    sidecar_url: str = "http://127.0.0.1:32190",
    session_id: str = "default",
    api_token: str = "",
    allowed_sender_refs: tuple[str, ...] = (),
) -> MessagingAccount:
    credentials = {
        "sidecar_url": sidecar_url,
        "session_id": session_id,
    }
    if api_token:
        credentials["api_token"] = api_token
    return await harness.account_service.create(
        character_id=character_id,
        platform=Platform.WHATSAPP,
        credentials=credentials,
        allowed_sender_refs=allowed_sender_refs,
    )


def _harness_tool_registry() -> InMemoryToolRegistry:
    return InMemoryToolRegistry([EchoTool(), FakeImageTool()])


def build_service_container(harness: MessagingHarness) -> ServiceContainer:
    """Wrap the harness into a ServiceContainer for route tests."""
    from kokoro_link.infrastructure.llm.fake import FakeChatModel
    from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
    model_registry = InMemoryChatModelRegistry(default_provider_id="fake")
    model_registry.register(FakeChatModel(provider_id="fake"))
    preferences = InMemoryPreferencesRepository()
    schedule_repository = InMemoryScheduleRepository()
    schedule_service = ScheduleService(
        repository=schedule_repository,
        planner=NullSchedulePlanner(),
        local_tz=timezone.utc,
    )
    active_llm_provider = PreferenceBackedActiveLLMProvider(
        registry=model_registry,
        preferences=preferences,
        default_provider_id="fake",
    )
    relationship_repository = InMemoryCharacterRelationshipRepository()
    encounter_repository = InMemoryCharacterEncounterRepository()
    relationship_service = CharacterRelationshipService(
        repository=relationship_repository,
        character_repository=harness.character_repository,
    )
    encounter_writer = CharacterEncounterMemoryWriter(
        repository=harness.memory_repository,
    )
    encounter_service = CharacterEncounterService(
        planner=CharacterEncounterPlanner(
            relationship_repository=relationship_repository,
            encounter_repository=encounter_repository,
            character_repository=harness.character_repository,
            schedule_service=schedule_service,
            schedule_repository=schedule_repository,
            provider=active_llm_provider,
            local_tz=timezone.utc,
        ),
        runner=CharacterEncounterRunner(
            encounter_repository=encounter_repository,
            character_repository=harness.character_repository,
            memory_writer=encounter_writer,
            relationship_service=relationship_service,
            provider=active_llm_provider,
        ),
        encounter_repository=encounter_repository,
    )
    storage = InMemoryObjectStorage(public_base_url="/uploads")
    return ServiceContainer(
        character_service=harness.character_service,
        chat_service=harness.chat_service,
        goal_service=GoalService(InMemoryGoalRepository()),
        schedule_service=schedule_service,
        character_draft_service=CharacterDraftService(
            generator=StubCharacterDraftGenerator(),
        ),
        companion_draft_service=CompanionDraftService(
            generator=StubCompanionDraftGenerator(),
            characters=harness.character_repository,
        ),
        character_image_service=CharacterImageService(
            character_repository=harness.character_repository,
            uploads_dir=Path(tempfile.mkdtemp(prefix="kokoro-test-uploads-")),
            object_storage=storage,
        ),
        character_lora_service=CharacterLoraService(
            character_repository=harness.character_repository,
            lora_dir=Path(tempfile.mkdtemp(prefix="kokoro-test-loras-")),
        ),
        character_relationship_service=relationship_service,
        character_encounter_service=encounter_service,
        album_service=AlbumService(
            album_repository=InMemoryAlbumRepository(),
            character_repository=harness.character_repository,
            uploads_dir=Path(tempfile.mkdtemp(prefix="kokoro-test-album-")),
            object_storage=storage,
        ),
        object_storage=storage,
        tool_registry=_harness_tool_registry(),
        tool_orchestrator=ToolOrchestrator(
            registry=_harness_tool_registry(),
            invocation_repository=InMemoryToolInvocationRepository(),
        ),
        tool_invocation_repository=InMemoryToolInvocationRepository(),
        memory_admin_service=MemoryAdminService(
            memory_repository=harness.memory_repository,
            embedder=None,
        ),
        memory_consolidation_service=MemoryConsolidationService(
            memory_repository=harness.memory_repository,
            consolidator=NullMemoryConsolidator(),
            embedder=None,
        ),
        state_history_repository=InMemoryStateHistoryRepository(),
        embedder=NullEmbedder(),
        provider_ids=model_registry.list_ids(),
        model_registry=model_registry,
        preferences_repository=preferences,
        story_seed_repository=InMemoryStorySeedRepository(),
        story_event_repository=InMemoryStoryEventRepository(),
        messaging_dispatcher=harness.dispatcher,
        messaging_account_service=harness.account_service,
        channel_binding_service=harness.binding_service,
        character_relationship_repository=relationship_repository,
        character_encounter_repository=encounter_repository,
    )


def build_messaging_app_client(
    harness: MessagingHarness,
) -> TestClient:
    app = FastAPI()
    app.state.container = build_service_container(harness)
    app.include_router(messaging_router, prefix="/api/v1")
    return TestClient(app)


def make_inbound(
    *,
    platform: Platform,
    account_id: str,
    chat_ref: str,
    text: str = "hi",
    sender_ref: str = "user-1",
    message_id: str = "m-1",
) -> InboundMessage:
    return InboundMessage(
        platform=platform,
        account_id=account_id,
        chat_ref=chat_ref,
        sender_ref=sender_ref,
        text=text,
        platform_message_id=message_id,
        received_at=datetime.now(timezone.utc),
    )
