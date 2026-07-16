from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.scene_access_service import SceneAccessService
from kokoro_link.contracts.scene_access import (
    SceneAccessContext,
    StageAccessAction,
    StageAccessDecision,
    StageAccessVerdict,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.conversation import Conversation, Message, MessageRole
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID, OperatorProfile
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.entities.schedule import MeetingAffordance, ScenePrivacy
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.presence_frame import AccessContext, ChatSurface
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_initial_relationship import (
    InMemoryCharacterOperatorRelationshipSeedRepository,
)


class _FakeJudge:
    def __init__(self, verdict: StageAccessVerdict | Exception) -> None:
        self.verdict = verdict
        self.seen: SceneAccessContext | None = None

    async def judge(self, context: SceneAccessContext) -> StageAccessVerdict:
        self.seen = context
        if isinstance(self.verdict, Exception):
            raise self.verdict
        return self.verdict


class _ScheduleService:
    def __init__(
        self,
        activity: ScheduleActivity | None,
        *,
        upcoming: list[ScheduleActivity] | None = None,
        just_finished: ScheduleActivity | None = None,
    ) -> None:
        self.activity = activity
        self.upcoming = upcoming or []
        self.just_finished = just_finished

    async def ensure_schedule(self, character: Character):  # noqa: ANN001
        return object()

    def resolve_current(self, schedule):  # noqa: ANN001
        return self.activity, self.upcoming, self.just_finished


class _OperatorProfileService:
    def __init__(
        self,
        *,
        primary_language: str = "zh-TW",
        current_status: str | None = None,
        current_status_set_at: datetime | None = None,
    ) -> None:
        self.primary_language = primary_language
        self.current_status = current_status
        self.current_status_set_at = current_status_set_at

    async def get_for_user(self, user_id: str) -> OperatorProfile:
        return OperatorProfile(
            id=user_id,
            display_name="操作者",
            primary_language=self.primary_language,
            timezone_id="Asia/Taipei",
            current_status=self.current_status,
            current_status_set_at=self.current_status_set_at,
        )


class _FailingRelationshipSeedRepository:
    async def get(self, character_id: str, operator_id: str):  # noqa: ANN001
        raise RuntimeError("seed lookup failed")


def _character(*, user_id: str = DEFAULT_OPERATOR_ID) -> Character:
    return Character.create(
        name="Mio",
        summary="溫和但重視界線的角色",
        user_id=user_id,
        personality=["細心"],
        interests=["閱讀"],
        speaking_style="自然",
        boundaries=["不喜歡被突然闖入私人空間"],
        state=CharacterState(
            emotion="平靜",
            affection=20,
            fatigue=10,
            trust=15,
            energy=70,
        ),
    )


async def _service(
    *,
    judge: _FakeJudge,
    activity: ScheduleActivity | None = None,
    upcoming: list[ScheduleActivity] | None = None,
    just_finished: ScheduleActivity | None = None,
    conversation_repository: InMemoryConversationRepository | None = None,
    operator_profile_service: _OperatorProfileService | None = None,
    relationship_seed_repository=None,  # noqa: ANN001
) -> tuple[SceneAccessService, Character]:
    repo = InMemoryCharacterRepository()
    character = _character()
    await repo.save(character)
    memories = InMemoryMemoryRepository()
    await memories.add(
        MemoryItem.create(
            character_id=character.id,
            kind=MemoryKind.RELATIONSHIP_MILESTONE,
            content="Mio 曾答應下次在車站附近見面。",
            salience=0.9,
        ),
    )
    return (
        SceneAccessService(
            character_repository=repo,
            judge=judge,
            schedule_service=_ScheduleService(
                activity,
                upcoming=upcoming,
                just_finished=just_finished,
            ),
            memory_repository=memories,
            operator_profile_service=operator_profile_service
            or _OperatorProfileService(),
            conversation_repository=conversation_repository,
            relationship_seed_repository=relationship_seed_repository,
        ),
        character,
    )


@pytest.mark.asyncio
async def test_scene_access_service_returns_judge_allow_and_passes_context() -> None:
    now = datetime.now(timezone.utc)
    activity = ScheduleActivity.create(
        start_at=now - timedelta(minutes=5),
        end_at=now + timedelta(minutes=55),
        description="在咖啡廳讀書",
        category="閱讀",
        location="咖啡廳",
        busy_score=0.2,
        scene_privacy=ScenePrivacy.PUBLIC,
        meeting_affordance=MeetingAffordance.OPEN_TO_ENCOUNTER,
    )
    judge = _FakeJudge(
        StageAccessVerdict(
            decision=StageAccessDecision.ALLOW,
            recommended_action=StageAccessAction.USE_STAGE,
            access_context=AccessContext.PUBLIC_ENCOUNTER,
            reason_for_user="她在開放場景，適合自然碰面。",
            prompt_fact="使用者與角色可在公共場景中合理相遇。",
        ),
    )
    service, character = await _service(judge=judge, activity=activity)

    verdict = await service.evaluate(
        character.id,
        operator_id=DEFAULT_OPERATOR_ID,
        requested_surface=ChatSurface.WEB_STAGE,
        current_user_id=DEFAULT_OPERATOR_ID,
    )

    assert verdict.access_context is AccessContext.PUBLIC_ENCOUNTER
    assert verdict.decision is StageAccessDecision.ALLOW
    assert judge.seen is not None
    assert judge.seen.current_activity_location == "咖啡廳"
    assert judge.seen.current_activity_summary == "在咖啡廳讀書"
    assert judge.seen.current_activity_scene_privacy == "public"
    assert judge.seen.current_activity_meeting_affordance == "open_to_encounter"
    assert judge.seen.operator_primary_language == "zh-TW"
    assert "已規劃活動段" in (judge.seen.schedule_context_summary or "")
    assert "車站附近見面" in "\n".join(judge.seen.recent_invitation_or_meetup_evidence)
    assert judge.seen.recent_dialogue == ()


@pytest.mark.asyncio
async def test_scene_access_service_passes_user_status_and_recent_dialogue() -> None:
    status_set_at = datetime(2026, 5, 29, 10, 30, tzinfo=timezone.utc)
    conversations = InMemoryConversationRepository()
    character = _character()
    conversation = Conversation.start(character_id=character.id).append(
        Message(
            role=MessageRole.USER,
            content="我今天被安排去你們學校演講。",
            created_at=status_set_at,
        ),
    ).append(
        Message(
            role=MessageRole.ASSISTANT,
            content="聽起來會很忙。",
            created_at=status_set_at + timedelta(minutes=1),
        ),
    )
    await conversations.save(conversation)

    judge = _FakeJudge(
        StageAccessVerdict(
            decision=StageAccessDecision.ALLOW,
            recommended_action=StageAccessAction.USE_STAGE,
            access_context=AccessContext.PUBLIC_ENCOUNTER,
            reason_for_user="使用者今天在同一所學校有公開活動。",
            prompt_fact="使用者可能是計畫外出現在學校；角色事前不知情，請自然演出驚訝。",
        ),
    )
    repo = InMemoryCharacterRepository()
    await repo.save(character)
    memories = InMemoryMemoryRepository()
    service = SceneAccessService(
        character_repository=repo,
        judge=judge,
        memory_repository=memories,
        operator_profile_service=_OperatorProfileService(
            current_status="正在校門口等演講",
            current_status_set_at=status_set_at,
        ),
        conversation_repository=conversations,
    )

    await service.evaluate(
        character.id,
        operator_id=DEFAULT_OPERATOR_ID,
        requested_surface=ChatSurface.WEB_STAGE,
        current_user_id=DEFAULT_OPERATOR_ID,
    )

    assert judge.seen is not None
    assert judge.seen.operator_current_status == "正在校門口等演講"
    assert judge.seen.operator_current_status_set_at == status_set_at
    assert any(
        "user: 我今天被安排去你們學校演講" in line
        for line in judge.seen.recent_dialogue
    )
    assert any(
        "assistant: 聽起來會很忙" in line
        for line in judge.seen.recent_dialogue
    )


@pytest.mark.asyncio
async def test_scene_access_service_passes_operator_primary_language() -> None:
    judge = _FakeJudge(
        StageAccessVerdict(
            decision=StageAccessDecision.BLOCK,
            recommended_action=StageAccessAction.USE_PHONE,
            access_context=AccessContext.TEXT_MESSAGE_ONLY,
            reason_for_user="It is better to text first.",
            prompt_fact="Use text messages; do not assume same-space presence.",
        ),
    )
    service, character = await _service(
        judge=judge,
        operator_profile_service=_OperatorProfileService(
            primary_language="en-US",
        ),
    )

    await service.evaluate(
        character.id,
        operator_id=DEFAULT_OPERATOR_ID,
        requested_surface=ChatSurface.WEB_STAGE,
        current_user_id=DEFAULT_OPERATOR_ID,
    )

    assert judge.seen is not None
    assert judge.seen.operator_primary_language == "en-US"


@pytest.mark.asyncio
async def test_scene_access_service_passes_initial_relationship_lines() -> None:
    relationship_repo = InMemoryCharacterOperatorRelationshipSeedRepository()
    judge = _FakeJudge(
        StageAccessVerdict(
            decision=StageAccessDecision.ALLOW,
            recommended_action=StageAccessAction.USE_STAGE,
            access_context=AccessContext.ESTABLISHED_ROUTINE,
            reason_for_user="她在共同住所的一般日常裡。",
            prompt_fact="使用者與角色同住；本輪是共同住所內的日常共處。",
        ),
    )
    service, character = await _service(
        judge=judge,
        relationship_seed_repository=relationship_repo,
    )
    await relationship_repo.save(
        CharacterOperatorRelationshipSeed(
            character_id=character.id,
            operator_id=DEFAULT_OPERATOR_ID,
            relationship_label="貼身小精靈",
            known_context="使用者確認她是剛創好的同住小精靈。",
            living_arrangement="住在使用者家裡。",
        ),
    )

    await service.evaluate(
        character.id,
        operator_id=DEFAULT_OPERATOR_ID,
        requested_surface=ChatSurface.WEB_STAGE,
        current_user_id=DEFAULT_OPERATOR_ID,
    )

    assert judge.seen is not None
    relationship_lines = "\n".join(judge.seen.initial_relationship_lines)
    assert "貼身小精靈" in relationship_lines
    assert "居住安排：住在使用者家裡" in relationship_lines


@pytest.mark.asyncio
async def test_scene_access_service_relationship_seed_failure_is_fail_soft() -> None:
    judge = _FakeJudge(
        StageAccessVerdict(
            decision=StageAccessDecision.BLOCK,
            recommended_action=StageAccessAction.USE_PHONE,
            access_context=AccessContext.TEXT_MESSAGE_ONLY,
            reason_for_user="先用文字比較自然。",
            prompt_fact="不要假設使用者已同場。",
        ),
    )
    service, character = await _service(
        judge=judge,
        relationship_seed_repository=_FailingRelationshipSeedRepository(),
    )

    await service.evaluate(
        character.id,
        operator_id=DEFAULT_OPERATOR_ID,
        requested_surface=ChatSurface.WEB_STAGE,
        current_user_id=DEFAULT_OPERATOR_ID,
    )

    assert judge.seen is not None
    assert judge.seen.initial_relationship_lines == ()


@pytest.mark.asyncio
async def test_scene_access_service_passes_gap_schedule_context_to_judge() -> None:
    now = datetime.now(timezone.utc)
    lunch = ScheduleActivity.create(
        start_at=now - timedelta(hours=2),
        end_at=now - timedelta(minutes=20),
        description="在公司附近吃午餐",
        category="meal",
        location="公司附近的餐館",
        busy_score=0.2,
        scene_privacy=ScenePrivacy.SEMI_PUBLIC,
        meeting_affordance=MeetingAffordance.OPEN_TO_ENCOUNTER,
    )
    practice = ScheduleActivity.create(
        start_at=now + timedelta(minutes=40),
        end_at=now + timedelta(hours=2),
        description="回工作室練琴",
        category="practice",
        location="Mio的工作室",
        busy_score=0.7,
        scene_privacy=ScenePrivacy.PRIVATE,
        meeting_affordance=MeetingAffordance.INVITE_ONLY,
    )
    judge = _FakeJudge(
        StageAccessVerdict(
            decision=StageAccessDecision.WARN,
            recommended_action=StageAccessAction.USE_PHONE,
            access_context=AccessContext.REMOTE_STAGE,
            reason_for_user="目前在行程空檔，先用遠端同場比較自然。",
            prompt_fact="角色目前不在明確活動段，不要假設使用者已到場。",
        ),
    )
    service, character = await _service(
        judge=judge,
        activity=None,
        upcoming=[practice],
        just_finished=lunch,
    )

    verdict = await service.evaluate(
        character.id,
        operator_id=DEFAULT_OPERATOR_ID,
        requested_surface=ChatSurface.WEB_STAGE,
        current_user_id=DEFAULT_OPERATOR_ID,
    )

    assert verdict.decision is StageAccessDecision.BLOCK
    assert verdict.recommended_action is StageAccessAction.USE_PHONE
    assert verdict.access_context is AccessContext.TEXT_MESSAGE_ONLY
    assert judge.seen is not None
    assert judge.seen.current_activity_summary is None
    summary = judge.seen.schedule_context_summary or ""
    assert "行程空檔" in summary
    assert "不能自動視為公共可抵達場景" in summary
    assert "在公司附近吃午餐" in summary
    assert "回工作室練琴" in summary
    assert "scene_privacy=private" in summary
    assert "meeting_affordance=invite_only" in summary


@pytest.mark.asyncio
async def test_scene_access_service_falls_back_to_phone_when_judge_fails() -> None:
    judge = _FakeJudge(RuntimeError("bad json"))
    service, character = await _service(judge=judge)

    verdict = await service.evaluate(
        character.id,
        operator_id=DEFAULT_OPERATOR_ID,
        requested_surface=ChatSurface.WEB_STAGE,
        current_user_id=DEFAULT_OPERATOR_ID,
    )

    assert verdict.decision is StageAccessDecision.BLOCK
    assert verdict.recommended_action is StageAccessAction.USE_PHONE
    assert verdict.access_context is AccessContext.TEXT_MESSAGE_ONLY
    assert verdict.suggested_opener


@pytest.mark.asyncio
async def test_scene_access_service_non_stage_surface_is_text_message_only() -> None:
    judge = _FakeJudge(
        StageAccessVerdict(
            decision=StageAccessDecision.ALLOW,
            recommended_action=StageAccessAction.USE_STAGE,
            access_context=AccessContext.PUBLIC_ENCOUNTER,
            reason_for_user="unused",
            prompt_fact="unused",
        ),
    )
    service, character = await _service(judge=judge)

    verdict = await service.evaluate(
        character.id,
        operator_id=DEFAULT_OPERATOR_ID,
        requested_surface=ChatSurface.WEB_DM,
        current_user_id=DEFAULT_OPERATOR_ID,
    )

    assert verdict.decision is StageAccessDecision.ALLOW
    assert verdict.recommended_action is StageAccessAction.USE_PHONE
    assert verdict.access_context is AccessContext.TEXT_MESSAGE_ONLY
    assert judge.seen is None
