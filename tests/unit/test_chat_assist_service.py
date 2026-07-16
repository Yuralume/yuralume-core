from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta, timezone
from typing import Sequence

import pytest

from kokoro_link.application.dto.character import CreateCharacterRequest
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.chat_assist_service import (
    ChatAssistCharacterNotFoundError,
    ChatAssistService,
)
from kokoro_link.application.services.feature_keys import FEATURE_CHAT_ASSIST
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessLocked,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.conversation import Conversation, Message, MessageRole
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.domain.entities.schedule import (
    MeetingAffordance,
    ScenePrivacy,
    ScheduleActivity,
)
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.entities.world_event import WorldEvent
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_world_events import (
    InMemoryWorldEventRepository,
)


class _ScriptedModel(ChatModelPort):
    provider_id = "scripted"
    supports_vision = False

    def __init__(self, response: str) -> None:
        self.response = response
        self.calls: list[tuple[str, str | None]] = []

    async def generate(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> str:
        self.calls.append((prompt, model))
        return self.response

    async def generate_stream(
        self,
        prompt: str,
        *,
        image_urls: Sequence[str] = (),
        model: str | None = None,
    ) -> AsyncIterator[str]:
        yield await self.generate(prompt, image_urls=image_urls, model=model)

    async def list_models(self) -> list[str]:
        return ["scripted-model"]


class _ActiveProvider:
    def __init__(self, model: _ScriptedModel, *, fake: bool = False) -> None:
        self.model = model
        self.fake = fake
        self.resolve_calls: list[str | None] = []
        self.fake_calls: list[str | None] = []

    async def resolve(self, feature_key=None, *, character=None):
        self.resolve_calls.append(feature_key)
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None):
        return "scripted-model"

    async def is_fake(self, feature_key=None, *, character=None):
        self.fake_calls.append(feature_key)
        return self.fake


class _ScheduleService:
    def __init__(self) -> None:
        now = datetime(2026, 5, 31, 9, 0, tzinfo=timezone.utc)
        self.current = ScheduleActivity.create(
            start_at=now,
            end_at=now + timedelta(hours=1),
            description="在咖啡店整理劇本分鏡",
            category="創作",
            location="巷口咖啡店",
            busy_score=0.35,
            scene_privacy=ScenePrivacy.SEMI_PUBLIC,
            meeting_affordance=MeetingAffordance.OPEN_TO_ENCOUNTER,
        )

    async def timezone_for_character(self, character):
        return timezone.utc

    async def ensure_schedule(self, character):
        return object()

    def resolve_current(self, schedule, *, upcoming_limit=3):
        return self.current, [], None

    async def today_for_character(self, character):
        return date(2026, 5, 31)


class _OperatorProfileService:
    async def get_for_user(self, user_id: str) -> OperatorProfile:
        return OperatorProfile(
            id=user_id,
            display_name="玩家",
            primary_language="zh-TW",
        )


async def _character_service() -> tuple[CharacterService, str]:
    character_repository = InMemoryCharacterRepository()
    service = CharacterService(character_repository)
    created = await service.create_character(
        CreateCharacterRequest(
            name="小雨",
            summary="喜歡在城市角落寫故事的少女。",
            personality=["敏銳", "有點怕打擾別人"],
            interests=["咖啡店", "劇本"],
            world_awareness_enabled=True,
            world_topics=["film"],
            subscribed_categories=["culture"],
        ),
    )
    return service, created.id


class _DenySubscriptionGuard:
    async def ensure_character_allowed(self, character) -> None:
        raise SubscriptionAccessLocked("tenant-a")


@pytest.mark.asyncio
async def test_subscription_lock_is_not_swallowed_by_chat_assist_fail_soft() -> None:
    character_service, character_id = await _character_service()
    provider = _ActiveProvider(_ScriptedModel('{"suggestions":[]}'))
    service = ChatAssistService(
        character_service=character_service,
        active_llm_provider=provider,
        subscription_access_guard=_DenySubscriptionGuard(),
    )

    with pytest.raises(SubscriptionAccessLocked):
        await service.suggest(character_id)

    assert provider.resolve_calls == []


@pytest.mark.asyncio
async def test_suggest_generates_contextual_player_lines() -> None:
    character_service, character_id = await _character_service()
    conversations = InMemoryConversationRepository()
    conversation = Conversation.start(character_id=character_id)
    conversation = conversation.append(
        Message(role=MessageRole.USER, content="昨天那個分鏡後來有想清楚嗎？"),
    ).append(
        Message(role=MessageRole.ASSISTANT, content="我還在猶豫結尾要不要留白。"),
    )
    await conversations.save(conversation)

    story_arcs = InMemoryStoryArcRepository()
    beat = StoryArcBeat.create(
        arc_id="arc-1",
        sequence=0,
        scheduled_date=date(2026, 5, 31),
        title="短片提案前夜",
        summary="她正在修改最後一版分鏡，怕作品太安靜。",
        location="咖啡店角落",
    )
    await story_arcs.add(
        StoryArc.create(
            id="arc-1",
            character_id=character_id,
            title="夏日短片企劃",
            premise="她要完成一支只拍城市聲音的短片。",
            theme="創作",
            start_date=date(2026, 5, 31),
            end_date=date(2026, 6, 20),
            beats=[beat],
        ),
    )

    world_events = InMemoryWorldEventRepository()
    # ``_load_world_events`` filters with ``max_age_days=7`` against the
    # real wall clock, so the event must be fresh relative to *now* (a
    # hardcoded 2026-05-31 falls outside the window once real time moves
    # past it). Stamp it "just now" so the freshness gate always passes
    # and the world-event context reaches the suggest prompt.
    event_published_at = datetime.now(timezone.utc)
    await world_events.upsert(
        WorldEvent(
            id="event-1",
            source="CultureWire",
            title="獨立短片影展公布入選名單",
            summary="今年的入選作品聚焦城市生活與日常聲音。",
            url="https://example.test/film",
            published_at=event_published_at,
            fetched_at=event_published_at,
            category="culture",
            topic_tags=("film",),
        ),
    )

    model = _ScriptedModel(
        """
        {"suggestions":[
          {"text":"你現在在咖啡店修分鏡嗎？我有點想聽你怎麼安排結尾。","reason":"承接行程與昨天的分鏡對話"},
          {"text":"看到影展名單那個新聞，我突然想到你的短片會不會也很適合那種日常聲音。","reason":"承接世界事件與劇情"},
          {"text":"如果結尾留白，你希望觀眾最後帶走的是安靜，還是某種沒說完的期待？","reason":"延續近期創作猶豫"}
        ]}
        """,
    )
    provider = _ActiveProvider(model)
    service = ChatAssistService(
        character_service=character_service,
        active_llm_provider=provider,  # type: ignore[arg-type]
        conversation_repository=conversations,
        schedule_service=_ScheduleService(),  # type: ignore[arg-type]
        story_arc_repository=story_arcs,
        world_event_repository=world_events,
        operator_profile_service=_OperatorProfileService(),  # type: ignore[arg-type]
    )

    response = await service.suggest(character_id, count=3)

    assert provider.fake_calls == [FEATURE_CHAT_ASSIST]
    assert provider.resolve_calls == [FEATURE_CHAT_ASSIST]
    assert len(response.suggestions) == 3
    assert response.suggestions[0].text.startswith("你現在在咖啡店")
    prompt, model_id = model.calls[0]
    assert model_id == "scripted-model"
    assert "在咖啡店整理劇本分鏡" in prompt
    assert "昨天那個分鏡後來有想清楚嗎？" in prompt
    assert "夏日短片企劃" in prompt
    assert "獨立短片影展公布入選名單" in prompt


@pytest.mark.asyncio
async def test_suggest_returns_empty_when_routed_to_fake_model() -> None:
    character_service, character_id = await _character_service()
    model = _ScriptedModel('{"suggestions":[{"text":"should not call"}]}')
    service = ChatAssistService(
        character_service=character_service,
        active_llm_provider=_ActiveProvider(model, fake=True),  # type: ignore[arg-type]
    )

    response = await service.suggest(character_id)

    assert response.suggestions == []
    assert model.calls == []


@pytest.mark.asyncio
async def test_suggest_collapses_cross_user_character_to_not_found() -> None:
    character_service, character_id = await _character_service()
    service = ChatAssistService(
        character_service=character_service,
        active_llm_provider=_ActiveProvider(_ScriptedModel("{}")),  # type: ignore[arg-type]
    )

    with pytest.raises(ChatAssistCharacterNotFoundError):
        await service.suggest(character_id, user_id="other-user")
