from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone, tzinfo

import pytest

from kokoro_link.application.dto.character import (
    CreateCharacterRequest,
    InitialRelationshipPayload,
)
from kokoro_link.application.dto.chat import (
    PresenceFramePayload,
    SendChatMessageRequest,
)
from kokoro_link.application.services.active_llm_provider import (
    PreferenceBackedActiveLLMProvider,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.application.services.scene_access_service import SceneAccessService
from kokoro_link.contracts.proactive import (
    GateVerdict,
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
)
from kokoro_link.contracts.scene_access import (
    SceneAccessContext,
    StageAccessAction,
    StageAccessDecision,
    StageAccessVerdict,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import (
    DailySchedule,
    MeetingAffordance,
    ScenePrivacy,
    ScheduleActivity,
)
from kokoro_link.domain.value_objects.presence_frame import (
    AccessContext,
    ChatSurface,
)
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.repositories.in_memory_channel_bindings import (
    InMemoryChannelBindingRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_initial_relationship import (
    InMemoryCharacterOperatorRelationshipSeedRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_messaging_accounts import (
    InMemoryMessagingAccountRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine


UTC = timezone.utc


class _SharedHomePlanner:
    def __init__(self) -> None:
        self.relationship_context = ""
        self.schedule_policy = ""

    async def plan_day(
        self,
        *,
        character: Character,
        date_: date,
        local_tz: tzinfo,
        operator_relationship_context: str = "",
        schedule_involvement_policy: str = "",
        **_: object,
    ) -> DailySchedule:
        self.relationship_context = operator_relationship_context
        self.schedule_policy = schedule_involvement_policy
        now = datetime.now(local_tz)
        activity = ScheduleActivity.create(
            start_at=now - timedelta(minutes=10),
            end_at=now + timedelta(minutes=50),
            description="在共同客廳整理小東西",
            category="home_routine",
            location="家（與使用者同住）",
            scene_privacy=ScenePrivacy.PRIVATE,
            meeting_affordance=MeetingAffordance.INVITE_ONLY,
        )
        return DailySchedule.create(
            character_id=character.id,
            date_=date_,
            activities=[activity],
        )


class _CohabitationSceneJudge:
    def __init__(self) -> None:
        self.seen: SceneAccessContext | None = None

    async def judge(self, context: SceneAccessContext) -> StageAccessVerdict:
        self.seen = context
        return StageAccessVerdict(
            decision=StageAccessDecision.ALLOW,
            recommended_action=StageAccessAction.USE_STAGE,
            access_context=AccessContext.ESTABLISHED_ROUTINE,
            reason_for_user="你們住在一起，現在是在共同住所的一般日常時段。",
            prompt_fact="同住設定讓共同住所的一般在家時段可作日常共處，但這不是共同回憶。",
        )


class _CapturingFakeChatModel(FakeChatModel):
    def __init__(self, provider_id: str) -> None:
        super().__init__(provider_id=provider_id)
        self.last_prompt = ""

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        self.last_prompt = prompt
        return await super().generate(
            prompt,
            image_urls=image_urls,
            model=model,
        )


class _OperatorProfileService:
    async def get_for_user(self, user_id: str) -> OperatorProfile:
        return OperatorProfile(
            id=user_id,
            display_name="操作者",
            primary_language="zh-TW",
            timezone_id="UTC",
        )


class _AllowGate:
    async def check(self, **_: object) -> GateVerdict:
        return GateVerdict(passed=True, reason="smoke")


class _CapturingDecider(ProactiveDeciderPort):
    def __init__(self) -> None:
        self.context: ProactiveContext | None = None

    async def decide(self, context: ProactiveContext) -> ProactiveDecision:
        self.context = context
        return ProactiveDecision(
            should_send=True,
            reason="shared-home context reached proactive without bypassing it",
            message="我先傳訊息問候你，等你方便再靠過來聊。",
        )


@pytest.mark.asyncio
async def test_cohabitation_creation_to_schedule_stage_chat_and_proactive_smoke() -> None:
    character_repository = InMemoryCharacterRepository()
    conversation_repository = InMemoryConversationRepository()
    memory_repository = InMemoryMemoryRepository()
    relationship_repository = InMemoryCharacterOperatorRelationshipSeedRepository()

    character_service = CharacterService(
        character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        relationship_seed_repository=relationship_repository,
    )
    created = await character_service.create_character(
        CreateCharacterRequest(
            name="露露",
            summary="貼身小精靈，住在使用者家中。",
            personality=["細心"],
            interests=["整理家裡的小物"],
            initial_relationship=InitialRelationshipPayload(
                relationship_label="貼身小精靈",
                known_context="知道使用者是她的主人，但沒有共同回憶。",
                living_arrangement="住在使用者家裡",
                schedule_involvement_policy="mention_only",
                proactive_permission=True,
                proactive_cadence_hint="一天最多一次，先用訊息問候。",
            ),
        ),
        user_id=DEFAULT_OPERATOR_ID,
    )
    character = await character_repository.get(created.id)
    assert character is not None

    seed = await relationship_repository.get(character.id, DEFAULT_OPERATOR_ID)
    assert seed is not None
    assert seed.living_arrangement == "住在使用者家裡"
    assert seed.schedule_involvement_policy == "mention_only"

    planner = _SharedHomePlanner()
    schedule_service = ScheduleService(
        repository=InMemoryScheduleRepository(),
        planner=planner,
        relationship_seed_repository=relationship_repository,
        local_tz=UTC,
    )
    schedule = await schedule_service.ensure_schedule(character)
    activity = schedule.activities[0]
    assert activity.location == "家（與使用者同住）"
    assert "居住安排：住在使用者家裡" in planner.relationship_context
    assert planner.schedule_policy == "mention_only"

    scene_judge = _CohabitationSceneJudge()
    scene_access = SceneAccessService(
        character_repository=character_repository,
        judge=scene_judge,
        schedule_service=schedule_service,
        memory_repository=memory_repository,
        conversation_repository=conversation_repository,
        relationship_seed_repository=relationship_repository,
    )
    verdict = await scene_access.evaluate(
        character.id,
        operator_id=DEFAULT_OPERATOR_ID,
        requested_surface=ChatSurface.WEB_STAGE,
        current_user_id=DEFAULT_OPERATOR_ID,
    )
    assert verdict.access_context is AccessContext.ESTABLISHED_ROUTINE
    assert scene_judge.seen is not None
    assert scene_judge.seen.current_activity_location == "家（與使用者同住）"
    assert "居住安排：住在使用者家裡" in "\n".join(
        scene_judge.seen.initial_relationship_lines,
    )

    chat_model = _CapturingFakeChatModel(provider_id="fake")
    model_registry = InMemoryChatModelRegistry(default_provider_id="fake")
    model_registry.register(chat_model)
    active_llm_provider = PreferenceBackedActiveLLMProvider(
        registry=model_registry,
        preferences=InMemoryPreferencesRepository(),
        default_provider_id="fake",
    )
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=NullPostTurnProcessor(),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=model_registry,
        state_engine=SimpleStateEngine(),
        active_llm_provider=active_llm_provider,
        relationship_seed_repository=relationship_repository,
        operator_profile_service=_OperatorProfileService(),
    )
    chat_response = await chat_service.send_message(
        SendChatMessageRequest(
            character_id=character.id,
            message="我先用手機問你，晚點再到客廳找你。",
            presence_frame=PresenceFramePayload.web_dm(),
        ),
    )
    assert "居住安排：住在使用者家裡" in chat_model.last_prompt
    assert "文字訊息" in chat_model.last_prompt
    assert chat_response.assistant_message is not None
    assert "剛剛一起" not in chat_response.assistant_message.content

    decider = _CapturingDecider()
    proactive_attempts = InMemoryProactiveAttemptRepository()

    async def schedule_resolver(character: Character, when: datetime):
        schedule = await schedule_service.get_schedule(character.id)
        if schedule is None:
            return None, [], None, None
        current, upcoming, just_finished = schedule_service.resolve_current(
            schedule,
            now=when,
        )
        return current, upcoming, schedule, just_finished

    proactive = ProactiveDispatcher(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        account_repository=InMemoryMessagingAccountRepository(),
        binding_repository=InMemoryChannelBindingRepository(),
        attempt_repository=proactive_attempts,
        gate=_AllowGate(),
        decider=decider,
        adapters={},
        schedule_resolver=schedule_resolver,
        schedule_service=schedule_service,
        relationship_seed_repository=relationship_repository,
        local_tz=UTC,
    )
    attempt = await proactive.evaluate(
        character_id=character.id,
        trigger=ProactiveTrigger.TICK,
        now=datetime.now(UTC) + timedelta(minutes=30),
    )
    assert attempt.outcome is ProactiveOutcome.SENT
    assert decider.context is not None
    assert decider.context.current_activity is not None
    assert decider.context.current_activity.location == "家（與使用者同住）"
    proactive_relationship = "\n".join(decider.context.initial_relationship_lines)
    assert "居住安排：住在使用者家裡" in proactive_relationship
    assert "一天最多一次" in proactive_relationship
    assert attempt.message == "我先傳訊息問候你，等你方便再靠過來聊。"
    assert "剛剛一起" not in attempt.message
