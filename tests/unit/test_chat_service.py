from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

import kokoro_link.application.services.chat_service as chat_module
from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.dto.chat import PresenceFramePayload, SendChatMessageRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import (
    ChatRuntimeLimitExceeded,
    ChatService,
)
from kokoro_link.application.services.feature_keys import FEATURE_NSFW_SAFE_SUMMARY
from kokoro_link.domain.entities.conversation import (
    Conversation, Message, MessageContentMode, MessageKind, MessageRole,
)
from kokoro_link.domain.entities.behavioral_pattern import (
    KIND_PHRASE_HABIT,
    KIND_RECURRING_ACTIVITY,
    BehavioralPattern,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.contracts.post_turn import (
    EmotionEventCandidate,
    PostTurnResult,
    StateSuggestion,
)
from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan
from kokoro_link.contracts.novelty_gate import NoveltyGateContext, NoveltyVerdict
from kokoro_link.contracts.register_profile import (
    RegisterProfile,
    RegisterProfileContext,
)
from kokoro_link.domain.entities.emotion_event import CAUSE_TURN
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.account_runtime_profile import AccountRuntimeProfile
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.presence_frame import (
    ChatChannel,
    ChatSurface,
    VisibilityMode,
)
from kokoro_link.infrastructure.observability.turn_recorder import BackgroundTurnRecorder
from kokoro_link.infrastructure.usage.recorder import BackgroundUsageEventRecorder
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.dialogue.llm_safe_summary import LLMNsfwSafeSummarizer
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_behavioral_patterns import (
    InMemoryBehavioralPatternRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import InMemoryCharacterRepository
from kokoro_link.infrastructure.repositories.in_memory_conversations import InMemoryConversationRepository
from kokoro_link.infrastructure.repositories.in_memory_emotion_events import (
    InMemoryEmotionEventRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_turn_records import (
    InMemoryTurnRecordRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_generation_usage import (
    InMemoryGenerationUsageRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine
from kokoro_link.domain.entities.generation_usage import STATUS_SUCCEEDED
from kokoro_link.contracts.prompt_material_digest import (
    PromptMaterialDigest,
    PromptMaterialDigestContext,
)


class _RecordingPromptBuilder:
    def __init__(self) -> None:
        self.last_recent_messages: list[Message] = []
        self.last_older_summary: str = ""
        self.last_kwargs: dict = {}
        self.calls: list[dict] = []

    def build(self, **kwargs) -> str:  # noqa: ANN003
        self.last_kwargs = dict(kwargs)
        self.calls.append(dict(kwargs))
        self.last_recent_messages = list(kwargs["recent_messages"])
        self.last_older_summary = (kwargs.get("older_dialogue_summary") or "").strip()
        latest = kwargs.get("latest_user_message", "")
        return f"最新使用者訊息：{latest}"


class _StubDialogueSummarizer:
    def __init__(self, summary: str) -> None:
        self.summary = summary
        self.calls: list[list[Message]] = []

    async def summarize(self, *, character, messages):  # noqa: ANN001
        self.calls.append(list(messages))
        return self.summary


class _StubPostTurnProcessor:
    """Returns canned memory items + state suggestion to exercise the pipeline."""

    def __init__(
        self,
        *,
        with_state: bool = False,
        emotion_events: list[EmotionEventCandidate] | None = None,
    ) -> None:
        self.calls: list[dict[str, object]] = []
        self._with_state = with_state
        self._emotion_events = emotion_events or []

    async def process(
        self,
        *,
        character,
        conversation_id,
        user_message,
        assistant_message,
        recent_messages=None,
        active_schedule=None,
        active_arc=None,
        operator=None,
        now=None,
    ):
        self.calls.append(
            {
                "character_id": character.id,
                "conversation_id": conversation_id,
                "user_message": user_message,
                "assistant_message": assistant_message,
                "active_schedule_date": (
                    getattr(active_schedule, "date", None)
                    if active_schedule is not None
                    else None
                ),
                "now": now,
            }
        )
        memories = [
            MemoryItem.create(
                character_id=character.id,
                conversation_id=conversation_id,
                kind=MemoryKind.SEMANTIC,
                content="使用者今天感到疲憊但仍願意交流",
                salience=0.7,
                tags=["mood", "energy"],
            )
        ]
        state_suggestion = (
            StateSuggestion(emotion="感動", affection_delta=3, trust_delta=2, energy_delta=-1)
            if self._with_state
            else None
        )
        return PostTurnResult(
            memories=memories,
            state_suggestion=state_suggestion,
            emotion_events=list(self._emotion_events),
        )


class _RecordingTTSPregenerator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.content_modes: list[MessageContentMode | str] = []

    async def pregenerate_if_enabled(
        self,
        *,
        character_id: str,
        text: str,
        user_id: str | None = None,
        content_mode: MessageContentMode | str = MessageContentMode.NORMAL,
    ) -> None:
        self.calls.append((character_id, text))
        self.content_modes.append(content_mode)


class _AlwaysNsfwModeService:
    async def content_mode_for_write(self, *, user_id: str) -> str:
        return "nsfw"

    async def refresh_activity(self, *, user_id: str) -> None:
        return None

    async def active_target(self, *, user_id: str):
        return SimpleNamespace(user_id=user_id)


class _RecordingNsfwSafeSummarizer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def summarize(
        self,
        *,
        character,
        message,
        model=None,
        model_id=None,
    ) -> str:
        self.calls.append({
            "character_id": character.id,
            "role": message.role,
            "content": message.content,
            "model_id": model_id,
            "provider_id": getattr(model, "provider_id", None),
        })
        return f"safe:{message.role.value}:{message.content[:8]}"


class _RecordingChatModel:
    provider_id = "openai"
    supports_vision = False

    def __init__(self, reply: str = "active model reply") -> None:
        self.reply = reply
        self.calls: list[dict[str, object]] = []

    async def generate(self, prompt: str, *, image_urls=(), model=None):  # noqa: ANN001
        self.calls.append({
            "prompt": prompt,
            "image_urls": tuple(image_urls),
            "model": model,
        })
        return self.reply

    async def generate_stream(self, prompt: str, *, image_urls=(), model=None):  # noqa: ANN001
        self.calls.append({
            "prompt": prompt,
            "image_urls": tuple(image_urls),
            "model": model,
        })
        yield self.reply

    async def list_models(self) -> list[str]:
        return ["text-embedding-3-small", "gpt-4o-mini"]


class _FailingChatModel(_RecordingChatModel):
    async def generate(self, prompt: str, *, image_urls=(), model=None):  # noqa: ANN001
        self.calls.append({
            "prompt": prompt,
            "image_urls": tuple(image_urls),
            "model": model,
        })
        raise RuntimeError("llm unavailable")


class _SequenceChatModel(_RecordingChatModel):
    def __init__(self, replies: list[str]) -> None:
        super().__init__(reply="")
        self.replies = list(replies)

    async def generate(self, prompt: str, *, image_urls=(), model=None):  # noqa: ANN001
        self.calls.append({
            "prompt": prompt,
            "image_urls": tuple(image_urls),
            "model": model,
        })
        if not self.replies:
            return ""
        return self.replies.pop(0)


class _ActiveProvider:
    def __init__(self, model: _RecordingChatModel, model_id: str | None) -> None:
        self.model = model
        self.model_id = model_id
        self.resolve_calls: list[dict[str, object]] = []

    async def resolve(self, feature_key=None, *, character=None):  # noqa: ANN001
        self.resolve_calls.append({
            "feature_key": feature_key,
            "character_id": getattr(character, "id", None),
        })
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None):  # noqa: ANN001
        return self.model_id

    async def is_fake(self, feature_key=None, *, character=None):  # noqa: ANN001
        return False


class _StaticRuntimeProfileResolver:
    def __init__(self, profile: AccountRuntimeProfile) -> None:
        self.profile = profile

    async def resolve_for_operator(self, operator_id: str) -> AccountRuntimeProfile:
        return self.profile


class _OverlayService:
    async def resolve_overlay(
        self, *, character_id: str, operator_id: str,
    ) -> dict[str, str]:
        return {"body_state": "off", "subjective_time": "off"}


class _OperatorProfileService:
    async def get_for_user(self, user_id: str):  # noqa: ANN001
        return SimpleNamespace(id=user_id, timezone_id="Asia/Taipei")

    async def get_current(self):  # noqa: ANN001
        return SimpleNamespace(id=DEFAULT_OPERATOR_ID, timezone_id="UTC")


class _PersonaService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def get_current(self, character_id: str, operator_id: str):
        self.calls.append((character_id, operator_id))
        return OperatorPersona.empty(character_id, operator_id)

    def render_for_prompt(self, persona):  # noqa: ANN001
        return ["關於對方：目前還不熟。"]


class _CuriosityContextService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.planned_calls: list[dict[str, object]] = []

    async def build_context(
        self,
        *,
        persona,
        surface: str,
        recent_dialogue_summary: str = "",
        initial_relationship_lines=(),
        now=None,
        operator_primary_language: str = "zh-TW",
    ):
        self.calls.append({
            "persona": persona,
            "surface": surface,
            "recent_dialogue_summary": recent_dialogue_summary,
            "initial_relationship_lines": initial_relationship_lines,
            "now": now,
            "operator_primary_language": operator_primary_language,
        })
        return SimpleNamespace(
            character_id=persona.character_id,
            operator_id=persona.operator_id,
            surface=surface,
        )

    async def record_planned_attempt(
        self,
        *,
        context,
        plan: PersonaCuriosityPlan,
        conversation_id: str | None = None,
        now=None,
    ):
        if not plan.should_ask:
            return None
        self.planned_calls.append({
            "context": context,
            "plan": plan,
            "conversation_id": conversation_id,
            "now": now,
        })
        return SimpleNamespace(id="planned-attempt")


class _CuriosityPlanner:
    def __init__(self, plan: PersonaCuriosityPlan) -> None:
        self._plan = plan
        self.calls: list[object] = []

    async def plan(self, context, *, character=None):  # noqa: ANN001
        self.calls.append({"context": context, "character": character})
        return self._plan


class _MaterialDigester:
    def __init__(self, digest: PromptMaterialDigest | None) -> None:
        self.result = digest
        self.calls: list[dict[str, object]] = []

    async def digest(self, context: PromptMaterialDigestContext, *, character=None):  # noqa: ANN001
        self.calls.append({"context": context, "character": character})
        return self.result


class _NoveltyGate:
    def __init__(self, verdicts: list[NoveltyVerdict]) -> None:
        self.verdicts = list(verdicts)
        self.calls: list[dict[str, object]] = []

    async def evaluate(self, context: NoveltyGateContext, *, character=None):  # noqa: ANN001
        self.calls.append({"context": context, "character": character})
        if not self.verdicts:
            return NoveltyVerdict(passes=True)
        return self.verdicts.pop(0)


class _RegisterProfiler:
    def __init__(self, profile: RegisterProfile | None) -> None:
        self.result = profile
        self.calls: list[dict[str, object]] = []

    async def profile(self, context: RegisterProfileContext, *, character=None):  # noqa: ANN001
        self.calls.append({"context": context, "character": character})
        return self.result


class _ScheduleForPostTurn:
    def __init__(self) -> None:
        self.calls: list[tuple[str, date | None]] = []

    async def get_schedule(self, character_id: str, *, date_: date | None = None):
        self.calls.append((character_id, date_))
        return SimpleNamespace(date=date_)


class _CapturingStoryArcService:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def ensure_active_arc(
        self,
        character,
        *,
        today=None,
        auto_start=True,
        open_new_season=True,
    ):
        self.calls.append(
            {
                "character_id": character.id,
                "today": today,
                "auto_start": auto_start,
                "open_new_season": open_new_season,
            }
        )
        return None


def _build_chat_service(
    *, processor=None,
) -> tuple[ChatService, CharacterService, InMemoryMemoryRepository, InMemoryCharacterRepository]:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))

    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=processor or NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    return chat_service, character_service, memory_repository, character_repository


def _build_runtime_limited_chat_service(
    *,
    max_messages_per_session: int,
) -> tuple[
    ChatService,
    CharacterService,
    InMemoryConversationRepository,
    _RecordingChatModel,
]:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    active_model = _RecordingChatModel(reply="demo reply")

    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        active_llm_provider=_ActiveProvider(active_model, "demo-allowed-model"),
        state_engine=SimpleStateEngine(),
        account_runtime_profile_resolver=_StaticRuntimeProfileResolver(
            AccountRuntimeProfile(
                name="demo-test",
                max_messages_per_session=max_messages_per_session,
            ),
        ),
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    return chat_service, character_service, conversation_repository, active_model


@pytest.mark.asyncio
async def test_runtime_profile_session_message_limit_blocks_next_turn() -> None:
    chat_service, character_service, conversation_repository, active_model = (
        _build_runtime_limited_chat_service(max_messages_per_session=1)
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    first = await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="first"),
    )

    with pytest.raises(ChatRuntimeLimitExceeded, match="session message limit"):
        await chat_service.send_message(
            SendChatMessageRequest(
                character_id=created.id,
                conversation_id=first.conversation_id,
                message="second",
            ),
        )

    conversation = await conversation_repository.get(first.conversation_id)
    assert conversation is not None
    assert [m.content for m in conversation.messages if m.role == MessageRole.USER] == [
        "first",
    ]
    assert len(active_model.calls) == 1


@pytest.mark.asyncio
async def test_runtime_profile_session_message_limit_blocks_stream_turn() -> None:
    chat_service, character_service, _conversation_repository, active_model = (
        _build_runtime_limited_chat_service(max_messages_per_session=1)
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )
    first = await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="first"),
    )

    with pytest.raises(ChatRuntimeLimitExceeded, match="session message limit"):
        await chat_service.send_message_stream(
            SendChatMessageRequest(
                character_id=created.id,
                conversation_id=first.conversation_id,
                message="second",
            ),
        )

    assert len(active_model.calls) == 1


@pytest.mark.asyncio
async def test_nsfw_mode_persists_safe_summaries_for_turn_messages() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    model = _RecordingChatModel(reply="這是助理回覆")
    active_provider = _ActiveProvider(model, "community-model")
    safe_summarizer = _RecordingNsfwSafeSummarizer()
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        active_llm_provider=active_provider,
        state_engine=SimpleStateEngine(),
        nsfw_mode_service=_AlwaysNsfwModeService(),
        nsfw_safe_summarizer=safe_summarizer,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="這段是限制級互動"),
    )
    await chat_service.wait_for_pending()

    conversation = await conversation_repository.latest_for_character(created.id)
    assert conversation is not None
    assert len(conversation.messages) == 2
    assert [m.content_mode for m in conversation.messages] == [
        MessageContentMode.NSFW,
        MessageContentMode.NSFW,
    ]
    assert conversation.messages[0].safe_summary.startswith("safe:user:")
    assert conversation.messages[1].safe_summary.startswith("safe:assistant:")
    assert [call["role"] for call in safe_summarizer.calls] == [
        MessageRole.USER,
        MessageRole.ASSISTANT,
    ]
    assert {call["model_id"] for call in safe_summarizer.calls} == {
        "community-model",
    }
    assert {call["provider_id"] for call in safe_summarizer.calls} == {
        "openai",
    }


@pytest.mark.asyncio
async def test_nsfw_safe_summary_generation_records_usage_rows() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    usage_repository = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_repository)
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    model = _RecordingChatModel(reply="安全摘要")
    active_provider = _ActiveProvider(model, "community-model")
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        active_llm_provider=active_provider,
        state_engine=SimpleStateEngine(),
        nsfw_mode_service=_AlwaysNsfwModeService(),
        nsfw_safe_summarizer=LLMNsfwSafeSummarizer(),
        usage_recorder=usage_recorder,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="這段是限制級互動"),
    )
    await chat_service.wait_for_pending()
    await usage_recorder.flush()

    conversation = await conversation_repository.latest_for_character(created.id)
    assert conversation is not None
    rows = await usage_repository.list_recent(limit=10)
    safe_rows = [
        row for row in rows
        if row.feature_key == FEATURE_NSFW_SAFE_SUMMARY
    ]
    assert len(safe_rows) == 2
    assert {row.conversation_id for row in safe_rows} == {conversation.id}
    assert {row.character_id for row in safe_rows} == {created.id}
    assert {row.operator_id for row in safe_rows} == {DEFAULT_OPERATOR_ID}
    assert {row.provider_id for row in safe_rows} == {"openai"}
    assert {row.model_id for row in safe_rows} == {"community-model"}
    assert {row.status for row in safe_rows} == {STATUS_SUCCEEDED}
    assert {row.source_surface for row in safe_rows} == {
        FEATURE_NSFW_SAFE_SUMMARY,
    }
    assert {row.routing_mode for row in safe_rows} == {"chat_safe_summary"}
    assert all(row.quantity.usage_unit == "token" for row in safe_rows)
    assert all(row.quantity.usage_is_estimated for row in safe_rows)
    assert all(
        row.metadata.get("metered_by") == "chat_service"
        for row in safe_rows
    )


@pytest.mark.asyncio
async def test_send_message_passes_presence_frame_to_prompt_builder() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    prompt_builder = _RecordingPromptBuilder()
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        state_engine=SimpleStateEngine(),
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    await chat_service.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="用私訊跟你說晚安",
            presence_frame=PresenceFramePayload(
                surface=ChatSurface.WEB_DM,
                channel=ChatChannel.KOKORO_DM,
                visibility=VisibilityMode.TEXT_ONLY,
                display_name="站內私訊",
            ),
        ),
    )

    frame = prompt_builder.last_kwargs["presence_frame"]
    assert frame.surface is ChatSurface.WEB_DM
    assert frame.channel is ChatChannel.KOKORO_DM
    assert frame.visibility is VisibilityMode.TEXT_ONLY


@pytest.mark.asyncio
async def test_send_message_passes_phrase_habits_to_prompt_builder() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    behavioral_repository = InMemoryBehavioralPatternRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    prompt_builder = _RecordingPromptBuilder()
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        behavioral_pattern_repository=behavioral_repository,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )
    await behavioral_repository.upsert(BehavioralPattern.new(
        character_id=created.id,
        kind=KIND_PHRASE_HABIT,
        description="句尾偶爾帶一個「欸」",
        observed_count=4,
        salience=0.8,
    ))
    await behavioral_repository.upsert(BehavioralPattern.new(
        character_id=created.id,
        kind=KIND_RECURRING_ACTIVITY,
        description="週一早上常去咖啡廳",
        observed_count=7,
        salience=0.9,
    ))

    await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="今天想聊聊"),
    )

    assert prompt_builder.last_kwargs["phrase_habit_lines"] == [
        "句尾偶爾帶一個「欸」",
    ]


@pytest.mark.asyncio
async def test_send_message_passes_persona_curiosity_plan_to_prompt_builder() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    prompt_builder = _RecordingPromptBuilder()
    persona_service = _PersonaService()
    curiosity_context = _CuriosityContextService()
    curiosity_planner = _CuriosityPlanner(
        PersonaCuriosityPlan(
            should_ask=True,
            target_layer=2,
            target_topic="companion_preference",
            tone_strategy="casual_self_disclosure",
            question_intent="learn how the user wants the character to respond",
            safety_reason="low pressure and relevant",
            avoid=("do not ask multiple questions",),
        ),
    )
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        operator_profile_service=_OperatorProfileService(),
        operator_persona_service=persona_service,
        persona_curiosity_service=curiosity_context,
        persona_curiosity_planner=curiosity_planner,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    await chat_service.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="今天有點累",
            operator_persona_enabled=True,
        ),
    )

    plan = prompt_builder.last_kwargs["persona_curiosity_plan"]
    assert plan is not None
    assert plan.target_topic == "companion_preference"
    assert curiosity_context.calls[0]["surface"] == "chat"
    assert curiosity_context.calls[0]["recent_dialogue_summary"] == ""
    assert len(curiosity_planner.calls) == 1
    assert curiosity_planner.calls[0]["character"].id == created.id
    assert len(curiosity_context.planned_calls) == 1
    assert curiosity_context.planned_calls[0]["plan"] == plan
    assert (
        curiosity_context.planned_calls[0]["conversation_id"]
        == prompt_builder.last_kwargs["conversation"].id
    )
    # Once for the ordinary persona prompt block, once for curiosity context.
    assert len(persona_service.calls) == 2


@pytest.mark.asyncio
async def test_send_message_skips_persona_curiosity_when_persona_disabled() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    prompt_builder = _RecordingPromptBuilder()
    persona_service = _PersonaService()
    curiosity_context = _CuriosityContextService()
    curiosity_planner = _CuriosityPlanner(PersonaCuriosityPlan(should_ask=True))
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        operator_profile_service=_OperatorProfileService(),
        operator_persona_service=persona_service,
        persona_curiosity_service=curiosity_context,
        persona_curiosity_planner=curiosity_planner,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    await chat_service.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="今天有點累",
            operator_persona_enabled=False,
        ),
    )

    assert prompt_builder.last_kwargs["persona_curiosity_plan"] is None
    assert curiosity_context.calls == []
    assert curiosity_context.planned_calls == []
    assert curiosity_planner.calls == []


@pytest.mark.asyncio
async def test_send_message_keeps_no_ask_persona_curiosity_plan_for_observability() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    prompt_builder = _RecordingPromptBuilder()
    curiosity_context = _CuriosityContextService()
    curiosity_planner = _CuriosityPlanner(
        PersonaCuriosityPlan(
            should_ask=False,
            safety_reason="recently asked a similar low-pressure topic",
        ),
    )
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        operator_profile_service=_OperatorProfileService(),
        operator_persona_service=_PersonaService(),
        persona_curiosity_service=curiosity_context,
        persona_curiosity_planner=curiosity_planner,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    await chat_service.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="今天有點累",
            operator_persona_enabled=True,
        ),
    )

    plan = prompt_builder.last_kwargs["persona_curiosity_plan"]
    assert plan is not None
    assert plan.should_ask is False
    assert "recently asked" in plan.safety_reason
    assert curiosity_context.planned_calls == []


@pytest.mark.asyncio
async def test_send_message_without_payload_model_uses_active_chat_provider() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    active_model = _RecordingChatModel()
    active_provider = _ActiveProvider(active_model, "gpt-4o-mini")
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        active_llm_provider=active_provider,
        state_engine=SimpleStateEngine(),
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    reply = await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="你好"),
    )

    assert reply.assistant_message is not None
    assert reply.assistant_message.content == "active model reply"
    assert active_provider.resolve_calls == [
        {"feature_key": "chat", "character_id": created.id},
    ]
    assert active_model.calls[0]["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_stream_message_without_payload_model_uses_active_chat_provider() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    active_model = _RecordingChatModel("streamed active reply")
    active_provider = _ActiveProvider(active_model, "gpt-4o-mini")
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        active_llm_provider=active_provider,
        state_engine=SimpleStateEngine(),
        usage_recorder=usage_recorder,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    token_stream, finalizer = await chat_service.send_message_stream(
        SendChatMessageRequest(character_id=created.id, message="你好"),
    )
    chunks = [chunk async for chunk in token_stream]
    response = await finalizer.finish("".join(chunks))
    await usage_recorder.flush()

    assert chunks == ["streamed active reply"]
    assert response.assistant_message is not None
    assert response.assistant_message.content == "streamed active reply"
    assert active_provider.resolve_calls == [
        {"feature_key": "chat", "character_id": created.id},
    ]
    assert active_model.calls[0]["model"] == "gpt-4o-mini"
    usage_rows = await usage_events.list_recent()
    assert len(usage_rows) == 1
    usage_row = usage_rows[0]
    assert usage_row.conversation_id == response.conversation_id
    assert usage_row.character_id == created.id
    assert usage_row.capability == "llm"
    assert usage_row.source_surface == "chat_stream"
    assert usage_row.model_id == "gpt-4o-mini"
    assert usage_row.quantity.prompt_tokens is not None
    assert usage_row.quantity.completion_tokens is not None
    assert usage_row.quantity.billable_quantity > 0


@pytest.mark.asyncio
async def test_tool_generation_no_tool_path_passes_experiment_overlay() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    model = FakeChatModel(provider_id="fake")
    registry.register(model)
    prompt_builder = _RecordingPromptBuilder()
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        experiment_overlay_service=_OverlayService(),
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    character = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )
    conversation = Conversation.start(character_id=character.id)

    await chat_service._generate_reply_with_tools(
        character=character,
        conversation=conversation,
        recent_messages=[],
        memories=[],
        pending_state=character.state,
        latest_user_message="今天想聊天",
        active_goals=[],
        current_activity=None,
        upcoming_activities=[],
        now=datetime.now(timezone.utc),
        idle_minutes=60,
        model=model,
        model_id=None,
    )

    assert prompt_builder.last_kwargs["experiment_overlay"] == {
        "body_state": "off",
        "subjective_time": "off",
    }


@pytest.mark.asyncio
async def test_send_message_creates_conversation_and_updates_state() -> None:
    chat_service, character_service, _, _ = _build_chat_service()

    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=["songs"])
    )

    reply = await chat_service.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            message="我今天有點累，但還是想跟你聊天",
        )
    )

    assert reply.conversation_id
    assert "我今天有點累" in reply.assistant_message.content
    assert reply.state.fatigue >= 1
    assert reply.state.trust >= 1


@pytest.mark.asyncio
async def test_send_message_records_replay_fields() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    turn_records = InMemoryTurnRecordRepository()
    turn_recorder = BackgroundTurnRecorder(turn_records)
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    prompt_builder = DefaultPromptContextBuilder()

    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        turn_recorder=turn_recorder,
        usage_recorder=usage_recorder,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    reply = await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="今天想聊天"),
    )
    await turn_recorder.flush()
    await usage_recorder.flush()

    records = await turn_records.list_recent(character_id=created.id)
    chat_record = next(r for r in records if r.kind == "chat")
    usage_rows = await usage_events.list_recent()
    usage_row = usage_rows[0]
    assert chat_record.model_id == "fake"
    assert "今天想聊天" in chat_record.prompt_assembled
    assert chat_record.prompt_pack_hash == prompt_builder.last_prompt_pack_hash
    assert chat_record.response_text == reply.assistant_message.content
    assert chat_record.latency_ms is not None
    assert chat_record.prompt_tokens is not None
    assert chat_record.prompt_tokens > 0
    assert chat_record.completion_tokens is not None
    assert chat_record.completion_tokens > 0
    assert chat_record.post_turn_refs["source"] == "send_message"
    assert chat_record.post_turn_refs["presence_frame"]["surface"] == "web_dm"
    assert chat_record.post_turn_refs["presence_frame"]["access_context"] == "text_message_only"
    assert usage_row.turn_record_id == chat_record.id
    assert usage_row.conversation_id == reply.conversation_id
    assert usage_row.character_id == created.id
    assert usage_row.operator_id == DEFAULT_OPERATOR_ID
    assert usage_row.capability == "llm"
    assert usage_row.feature_key == "chat"
    assert usage_row.provider_id == "fake"
    assert usage_row.model_id == "fake"
    assert usage_row.prompt_pack_hash == prompt_builder.last_prompt_pack_hash
    assert usage_row.quantity.usage_unit == "token"
    assert usage_row.quantity.prompt_tokens == chat_record.prompt_tokens
    assert usage_row.quantity.completion_tokens == chat_record.completion_tokens
    assert usage_row.quantity.billable_quantity == (
        chat_record.prompt_tokens + chat_record.completion_tokens
    )
    assert usage_row.quantity.usage_is_estimated is True
    assert usage_row.cost.is_estimated is True
    assert usage_row.metadata["aggregate"] is True


@pytest.mark.asyncio
async def test_send_message_applies_prompt_material_digest_and_records_refs() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    turn_records = InMemoryTurnRecordRepository()
    turn_recorder = BackgroundTurnRecorder(turn_records)
    prompt_builder = _RecordingPromptBuilder()
    digest = PromptMaterialDigest(
        bullets=("角色記得使用者昨天說過的事。",),
        digest_metadata={
            "provider_id": "unit-provider",
            "model_id": "digest-model",
            "latency_ms": 12,
            "prompt_tokens": 30,
            "completion_tokens": 8,
            "error": None,
        },
    )
    digester = _MaterialDigester(digest)
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        turn_recorder=turn_recorder,
        prompt_material_digester=digester,
        prompt_material_digest_enabled=True,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="今天想聊天"),
    )
    await turn_recorder.flush()

    assert len(digester.calls) == 1
    context = digester.calls[0]["context"]
    assert isinstance(context, PromptMaterialDigestContext)
    assert context.character_id == created.id
    assert prompt_builder.last_kwargs["material_digest"] is digest
    records = await turn_records.list_recent(character_id=created.id)
    chat_record = next(r for r in records if r.kind == "chat")
    material_refs = chat_record.post_turn_refs["material_digest"]
    assert material_refs["enabled"] is True
    assert material_refs["applied"] is True
    assert material_refs["bullet_count"] == 1
    assert material_refs["provider_id"] == "unit-provider"
    assert material_refs["model_id"] == "digest-model"


@pytest.mark.asyncio
async def test_prompt_material_digest_disabled_does_not_call_digester() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    prompt_builder = _RecordingPromptBuilder()
    digester = _MaterialDigester(PromptMaterialDigest(bullets=("unused",)))
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        prompt_material_digester=digester,
        prompt_material_digest_enabled=False,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="今天想聊天"),
    )

    assert digester.calls == []
    assert prompt_builder.last_kwargs["material_digest"] is None


@pytest.mark.asyncio
async def test_send_message_retries_when_novelty_gate_fails_and_records_refs() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    turn_records = InMemoryTurnRecordRepository()
    turn_recorder = BackgroundTurnRecorder(turn_records)
    prompt_builder = _RecordingPromptBuilder()
    active_model = _SequenceChatModel(["flat reply", "fresh reply"])
    active_provider = _ActiveProvider(active_model, "gpt-4o-mini")
    novelty_gate = _NoveltyGate([
        NoveltyVerdict(
            passes=False,
            lacks_novelty=True,
            feedback="補一件此刻的小事",
            gate_metadata={"provider_id": "gate-provider"},
        ),
    ])
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        active_llm_provider=active_provider,
        state_engine=SimpleStateEngine(),
        turn_recorder=turn_recorder,
        novelty_gate=novelty_gate,
        novelty_gate_enabled=True,
        novelty_gate_max_retries=1,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    reply = await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="今天想聊天"),
    )
    await turn_recorder.flush()

    assert reply.assistant_message is not None
    assert reply.assistant_message.content == "fresh reply"
    assert len(active_model.calls) == 2
    assert len(novelty_gate.calls) == 1
    assert prompt_builder.calls[0]["retry_directive"] is None
    assert prompt_builder.calls[1]["retry_directive"] == "補一件此刻的小事"
    gate_context = novelty_gate.calls[0]["context"]
    assert isinstance(gate_context, NoveltyGateContext)
    assert gate_context.response_text == "flat reply"
    records = await turn_records.list_recent(character_id=created.id)
    chat_record = next(r for r in records if r.kind == "chat")
    novelty_refs = chat_record.post_turn_refs["novelty_gate"]
    assert novelty_refs["enabled"] is True
    assert novelty_refs["passes"] is False
    assert novelty_refs["lacks_novelty"] is True
    assert novelty_refs["retry_count"] == 1
    assert novelty_refs["provider_id"] == "gate-provider"


@pytest.mark.asyncio
async def test_stream_message_buffers_retry_when_novelty_gate_fails() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    turn_records = InMemoryTurnRecordRepository()
    turn_recorder = BackgroundTurnRecorder(turn_records)
    prompt_builder = _RecordingPromptBuilder()
    active_model = _SequenceChatModel(["flat stream candidate", "fresh stream reply"])
    active_provider = _ActiveProvider(active_model, "gpt-4o-mini")
    novelty_gate = _NoveltyGate([
        NoveltyVerdict(
            passes=False,
            imagery_relapse=True,
            feedback="避開熟悉意象，換一個具體反應",
        ),
    ])
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        active_llm_provider=active_provider,
        state_engine=SimpleStateEngine(),
        turn_recorder=turn_recorder,
        novelty_gate=novelty_gate,
        novelty_gate_enabled=True,
        novelty_gate_max_retries=1,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    token_stream, finalizer = await chat_service.send_message_stream(
        SendChatMessageRequest(character_id=created.id, message="今天想聊天"),
    )
    chunks = [chunk async for chunk in token_stream]
    response = await finalizer.finish("".join(chunks))
    await turn_recorder.flush()

    assert chunks == ["fresh stream reply"]
    assert response.assistant_message is not None
    assert response.assistant_message.content == "fresh stream reply"
    assert len(active_model.calls) == 2
    assert len(novelty_gate.calls) == 1
    assert prompt_builder.calls[1]["retry_directive"] == "避開熟悉意象，換一個具體反應"
    records = await turn_records.list_recent(character_id=created.id)
    chat_record = next(r for r in records if r.kind == "chat")
    novelty_refs = chat_record.post_turn_refs["novelty_gate"]
    assert novelty_refs["enabled"] is True
    assert novelty_refs["passes"] is False
    assert novelty_refs["imagery_relapse"] is True
    assert novelty_refs["retry_count"] == 1


@pytest.mark.asyncio
async def test_stream_message_keeps_incremental_stream_for_low_risk_reply_quality_gate() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    turn_records = InMemoryTurnRecordRepository()
    turn_recorder = BackgroundTurnRecorder(turn_records)
    prompt_builder = _RecordingPromptBuilder()
    active_model = _RecordingChatModel("low risk stream reply")
    active_provider = _ActiveProvider(active_model, "gpt-4o-mini")
    novelty_gate = _NoveltyGate([NoveltyVerdict(passes=False, over_warm=True)])
    register_profiler = _RegisterProfiler(
        RegisterProfile(
            axes={
                "emotional_intensity": 0.1,
                "seriousness": 0.1,
                "intimacy": 0.2,
                "humor_latitude": 0.5,
                "help_seeking": 0.0,
            },
            confidence=0.9,
            vulnerable_disclosure=False,
            note="日常閒聊",
        ),
    )
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        active_llm_provider=active_provider,
        state_engine=SimpleStateEngine(),
        turn_recorder=turn_recorder,
        register_profiler=register_profiler,
        register_profile_enabled=True,
        novelty_gate=novelty_gate,
        novelty_gate_enabled=True,
        reply_quality_gate_risk_threshold=0.9,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    token_stream, finalizer = await chat_service.send_message_stream(
        SendChatMessageRequest(character_id=created.id, message="今天想聊天"),
    )
    chunks = [chunk async for chunk in token_stream]
    response = await finalizer.finish("".join(chunks))
    await turn_recorder.flush()

    assert chunks == ["low risk stream reply"]
    assert response.assistant_message is not None
    assert response.assistant_message.content == "low risk stream reply"
    assert len(novelty_gate.calls) == 0
    assert len(active_model.calls) == 1
    assert prompt_builder.last_kwargs["turn_register_profile"] is register_profiler.result
    assert prompt_builder.last_kwargs["reply_diversity_evidence"].assistant_line_count == 0
    records = await turn_records.list_recent(character_id=created.id)
    chat_record = next(r for r in records if r.kind == "chat")
    assert chat_record.post_turn_refs["register_profile"]["applied"] is True
    assert chat_record.post_turn_refs["diversity"]["assistant_line_count"] == 0
    novelty_refs = chat_record.post_turn_refs["novelty_gate"]
    assert novelty_refs["enabled"] is True
    assert novelty_refs["evaluated"] is False
    assert novelty_refs["passes"] is True


@pytest.mark.asyncio
async def test_send_message_records_failed_usage_when_llm_call_raises() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    usage_events = InMemoryGenerationUsageRepository()
    usage_recorder = BackgroundUsageEventRecorder(usage_events)
    active_model = _FailingChatModel()
    active_provider = _ActiveProvider(active_model, "gpt-4o-mini")
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        active_llm_provider=active_provider,
        state_engine=SimpleStateEngine(),
        usage_recorder=usage_recorder,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    with pytest.raises(RuntimeError, match="llm unavailable"):
        await chat_service.send_message(
            SendChatMessageRequest(character_id=created.id, message="今天想聊天"),
        )
    await usage_recorder.flush()

    rows = await usage_events.list_recent()
    assert len(rows) == 1
    row = rows[0]
    assert row.turn_record_id is None
    assert row.conversation_id
    assert row.character_id == created.id
    assert row.operator_id == DEFAULT_OPERATOR_ID
    assert row.capability == "llm"
    assert row.feature_key == "chat"
    assert row.provider_id == "openai"
    assert row.model_id == "gpt-4o-mini"
    assert row.status == "failed"
    assert row.error_code == "llm_error"
    assert "llm unavailable" in (row.error_message or "")
    assert row.quantity.usage_unit == "token"
    assert row.quantity.usage_is_estimated is True
    assert row.quantity.billable_quantity == 0


@pytest.mark.asyncio
async def test_rich_emotion_event_records_without_state_suggestion_and_refs_turn() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    turn_records = InMemoryTurnRecordRepository()
    turn_recorder = BackgroundTurnRecorder(turn_records)
    emotion_events = InMemoryEmotionEventRepository()
    processor = _StubPostTurnProcessor(
        emotion_events=[
            EmotionEventCandidate(
                emotion_label="安心",
                evidence_quote="我今天想聊天",
                valence=0.7,
                arousal=0.2,
                intensity=0.8,
                trust_delta=2,
            ),
        ],
    )

    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=processor,
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        turn_recorder=turn_recorder,
        emotion_event_repository=emotion_events,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="我今天想聊天"),
    )
    await turn_recorder.flush()

    records = await turn_records.list_recent(character_id=created.id)
    chat_record = next(r for r in records if r.kind == "chat")
    events = await emotion_events.list_recent(
        character_id=created.id,
        operator_id=DEFAULT_OPERATOR_ID,
        since=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    assert len(events) == 1
    assert events[0].cause_ref_kind == CAUSE_TURN
    assert events[0].cause_ref_id == chat_record.id
    assert events[0].emotion_label == "安心"
    assert events[0].trust_delta == 2
    assert chat_record.post_turn_refs["emotion_event_ids"] == [events[0].id]


@pytest.mark.asyncio
async def test_send_message_schedules_tts_pregeneration_after_assistant_reply() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    tts_pregenerator = _RecordingTTSPregenerator()

    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        tts_pregenerator=tts_pregenerator,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Mio", personality=["kind"], interests=[]),
    )

    reply = await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="今天想聊天"),
    )
    await chat_service.wait_for_pending()

    assert tts_pregenerator.calls == [
        (created.id, reply.assistant_message.content),
    ]
    assert tts_pregenerator.content_modes == [MessageContentMode.NORMAL]


@pytest.mark.asyncio
async def test_send_message_persists_extracted_memories() -> None:
    processor = _StubPostTurnProcessor()
    chat_service, character_service, memory_repository, _ = _build_chat_service(processor=processor)

    created = await character_service.create_character(
        CreateCharacterRequest(name="Airi", personality=["gentle"], interests=[])
    )

    await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="最近開始學吉他"),
    )

    assert len(processor.calls) == 1
    call = processor.calls[0]
    assert call["character_id"] == created.id
    assert call["user_message"] == "最近開始學吉他"

    stored = await memory_repository.query(created.id, limit=10)
    assert len(stored) == 1
    assert stored[0].kind == MemoryKind.SEMANTIC
    assert stored[0].content == "使用者今天感到疲憊但仍願意交流"
    assert stored[0].salience == pytest.approx(0.7)


@pytest.mark.asyncio
async def test_post_turn_applies_state_suggestion() -> None:
    processor = _StubPostTurnProcessor(with_state=True)
    chat_service, character_service, _, character_repository = _build_chat_service(processor=processor)

    created = await character_service.create_character(
        CreateCharacterRequest(name="Sora", personality=["cheerful"], interests=[])
    )
    original_affection = created.state.affection

    await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="今天謝謝你陪我"),
    )

    # After post-turn, state should reflect the LLM suggestion
    refined = await character_repository.get(created.id)
    assert refined is not None
    assert refined.state.emotion == "感動"
    # affection should have both heuristic + LLM delta applied
    assert refined.state.affection > original_affection


@pytest.mark.asyncio
async def test_post_turn_loads_active_schedule_in_owner_timezone(monkeypatch) -> None:
    class _FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ANN001
            fixed = datetime(2026, 6, 14, 16, 30, tzinfo=timezone.utc)
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr(chat_module, "datetime", _FixedDateTime)

    processor = _StubPostTurnProcessor()
    schedule_service = _ScheduleForPostTurn()
    chat_service = ChatService(
        character_repository=InMemoryCharacterRepository(),
        conversation_repository=InMemoryConversationRepository(),
        memory_repository=InMemoryMemoryRepository(),
        post_turn_processor=processor,
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=InMemoryChatModelRegistry(default_provider_id="fake"),
        state_engine=SimpleStateEngine(),
        schedule_service=schedule_service,  # type: ignore[arg-type]
        operator_profile_service=_OperatorProfileService(),  # type: ignore[arg-type]
    )
    character = Character.create(
        name="Mio",
        summary="",
        user_id="alice",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )

    await chat_service._do_post_turn(
        character=character,
        conversation_id="conv-1",
        turn_record_id="turn-1",
        user_text="今天晚上要忙",
        assistant_text="我記得了",
        prior_messages=[],
    )

    assert schedule_service.calls == [(character.id, date(2026, 6, 15))]
    assert processor.calls[-1]["active_schedule_date"] == date(2026, 6, 15)


@pytest.mark.asyncio
async def test_chat_story_arc_prompt_load_does_not_open_new_season() -> None:
    arc_service = _CapturingStoryArcService()
    chat_service = ChatService(
        character_repository=InMemoryCharacterRepository(),
        conversation_repository=InMemoryConversationRepository(),
        memory_repository=InMemoryMemoryRepository(),
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=InMemoryChatModelRegistry(default_provider_id="fake"),
        state_engine=SimpleStateEngine(),
        story_arc_service=arc_service,  # type: ignore[arg-type]
    )
    character = Character.create(
        name="Mio",
        summary="",
        user_id="alice",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )

    arc, beats = await chat_service._ensure_story_arc(
        character,
        today=date(2026, 6, 4),
    )

    assert arc is None
    assert beats == []
    assert arc_service.calls == [
        {
            "character_id": character.id,
            "today": date(2026, 6, 4),
            "auto_start": True,
            "open_new_season": False,
        }
    ]


@pytest.mark.asyncio
async def test_deduplication_prevents_duplicate_memories() -> None:
    processor = _StubPostTurnProcessor()
    chat_service, character_service, memory_repository, _ = _build_chat_service(processor=processor)

    created = await character_service.create_character(
        CreateCharacterRequest(name="Yui", personality=["shy"], interests=[])
    )

    # First message — memory should be stored
    await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="我好累"),
    )
    stored = await memory_repository.query(created.id, limit=10)
    assert len(stored) == 1

    # Second message — same processor returns same memory content, should be deduped
    await chat_service.send_message(
        SendChatMessageRequest(character_id=created.id, message="還是很累"),
    )
    stored = await memory_repository.query(created.id, limit=10)
    assert len(stored) == 1  # still 1, duplicate was filtered


@pytest.mark.asyncio
async def test_prompt_keeps_last_three_raw_messages_and_summarises_older_dialogue() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    prompt_builder = _RecordingPromptBuilder()
    summarizer = _StubDialogueSummarizer("這是較舊對話摘要")

    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=prompt_builder,
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        dialogue_summarizer=summarizer,
    )
    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(name="Nana", personality=["calm"], interests=[]),
    )

    conversation = Conversation.start(character_id=created.id)
    seeded = [
        Message(role=MessageRole.USER, content="m1"),
        Message(
            role=MessageRole.ASSISTANT,
            content="",
            kind=MessageKind.TOOL_ONLY,
        ),
        Message(role=MessageRole.USER, content="m3"),
        Message(role=MessageRole.ASSISTANT, content="m4"),
        Message(role=MessageRole.USER, content="m5"),
        Message(role=MessageRole.ASSISTANT, content="m6"),
        Message(role=MessageRole.USER, content="m7"),
        Message(role=MessageRole.ASSISTANT, content="m8"),
    ]
    for msg in seeded:
        conversation = conversation.append(msg)
    await conversation_repository.save(conversation)

    await chat_service.send_message(
        SendChatMessageRequest(
            character_id=created.id,
            conversation_id=conversation.id,
            message="本輪訊息",
        ),
    )

    assert [m.content for m in prompt_builder.last_recent_messages] == ["m6", "m7", "m8"]
    assert prompt_builder.last_older_summary == "這是較舊對話摘要"
    assert len(summarizer.calls) == 1
    assert [m.content for m in summarizer.calls[0]] == ["m1", "m3", "m4", "m5"]


def test_nsfw_content_mode_tags_memories_before_storage() -> None:
    memory = MemoryItem.create(
        character_id="c1",
        conversation_id="conv-1",
        kind=MemoryKind.EPISODIC,
        content="關係更進一步，彼此更信任。",
        tags=("relationship",),
    )

    tagged = chat_module._with_nsfw_memory_tags([memory], "nsfw")

    assert tagged[0] is not memory
    assert tagged[0].tags == ("relationship", "content_mode:nsfw")


def test_normal_content_mode_keeps_memory_tags_unchanged() -> None:
    memory = MemoryItem.create(
        character_id="c1",
        conversation_id="conv-1",
        kind=MemoryKind.EPISODIC,
        content="一起吃了晚餐。",
        tags=("daily",),
    )

    tagged = chat_module._with_nsfw_memory_tags([memory], "normal")

    assert tagged == [memory]
