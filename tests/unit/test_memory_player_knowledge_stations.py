"""KB6 — every write station stamps ``MemoryItem.player_knowledge``.

The value is a *structural* fact about the pathway that produced the
memory, never a judgement about its text: a chat turn happened with the
player in the room, an encounter happened between two characters while
the player was elsewhere. So each test here drives a station and asserts
the verdict it stamps — no test inspects or crafts memory *content* to
steer the answer, because no production code does either.

One test per station on the plan's 12-row table (§3.2 KB6), plus the
propagation rule for ``memory_consolidation_service``, which is not a
station: it rewrites clusters and must inherit the most *protective*
verdict present rather than mint a new one — ``private`` first of all,
ahead of the unjudged ``""``, which renders identically to ``shared``.

Stations whose two table rows share one code path (story_event_service's
"beat realize" and "autonomous story event") are covered by driving both
paths, so the shared derivation is pinned from both sides.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from kokoro_link.application.services.character_encounter_service import (
    CharacterEncounterMemoryWriter,
    EncounterReflection,
    ReflectionMemoryEntry,
)
from kokoro_link.application.services.character_social_knowledge_service import (
    PeerKnowledgeSeed,
    _seed_memory,
)
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.feed_comment_reply_service import (
    _build_reply_memory,
)
from kokoro_link.application.services.feed_composer_service import (
    _post_to_memory,
)
from kokoro_link.application.services.feed_reaction_memorializer import (
    FeedReactionMemorializer,
)
from kokoro_link.application.services.memory_consolidation_service import (
    MemoryConsolidationService,
)
from kokoro_link.application.services.relationship_milestone_service import (
    RelationshipMilestoneService,
)
from kokoro_link.application.services.schedule_memorializer import (
    ScheduleMemorializer,
)
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_event_service import (
    StoryEventService,
    _player_knowledge_for_story_memory,
)
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.application.services.story_scene_closing import (
    SceneClosingCoordinator,
)
from kokoro_link.bootstrap.settings import HumanizationSettings
from kokoro_link.contracts.activity_aftermath import ActivityAftermath
from kokoro_link.contracts.memory_consolidator import MergeProposal
from kokoro_link.contracts.post_turn import ArcAdjustmentSignal, PostTurnResult
from kokoro_link.contracts.story import StoryEventExpanderPort
from kokoro_link.contracts.story_arc import StoryArcPlannerPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageRole,
)
from kokoro_link.domain.entities.feed_comment import FeedComment
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.feed_reaction import FeedReaction
from kokoro_link.domain.entities.memory_item import (
    PLAYER_KNOWLEDGE_DISCLOSED,
    PLAYER_KNOWLEDGE_PRIVATE,
    PLAYER_KNOWLEDGE_SHARED,
    MemoryItem,
    merge_player_knowledge,
)
from kokoro_link.domain.entities.operator_persona import InteractionStrength
from kokoro_link.domain.entities.schedule import (
    OPERATOR_CONFIRMED_SHARED_ROLE,
    OPERATOR_INVITE_PENDING_ROLE,
    DailySchedule,
    ScheduleActivity,
)
from kokoro_link.domain.entities.story_arc import (
    OPERATOR_POSITION_ABSENT,
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
    StoryArc,
    StoryArcBeat,
    TENSION_SETUP,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_LAYER_SIDE_STORY,
    StorySceneSession,
)
from kokoro_link.domain.entities.story_seed import StorySeed
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.familiarity import Familiarity
from kokoro_link.domain.value_objects.feed_kind import FeedKind
from kokoro_link.domain.value_objects.feed_source import FeedSource
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.memory.llm_extractor import (
    _payload_to_item as _extractor_payload_to_item,
)
from kokoro_link.infrastructure.post_turn.llm_processor import (
    _payload_to_item as _post_turn_payload_to_item,
)
from kokoro_link.infrastructure.prompt.default import DefaultPromptContextBuilder
from kokoro_link.infrastructure.prompt.memory_lines import (
    PLAYER_UNAWARE_FRAME,
    memory_knowledge_frame,
)
from kokoro_link.infrastructure.repositories.in_memory_characters import (
    InMemoryCharacterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import (
    InMemoryConversationRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_comments import (
    InMemoryFeedCommentRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_reactions import (
    InMemoryFeedReactionRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_schedules import (
    InMemoryScheduleRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
    InMemoryStorySeedRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)
from kokoro_link.infrastructure.state.simple import SimpleStateEngine

UTC = timezone.utc
_NOW = datetime(2026, 8, 26, 12, 0, tzinfo=UTC)


def _character(*, name: str = "亞米", last_active_at: datetime | None = None) -> Character:
    character = Character.create(
        name=name,
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="平靜", affection=60, fatigue=20, trust=60, energy=80,
            last_active_at=last_active_at,
        ),
    )
    return replace(character, user_id="owner-1")


# ── station 1: chat-turn memory extractor ────────────────────────────


def test_llm_extractor_stamps_shared() -> None:
    """The extractor only ever runs on a completed user/assistant turn,
    so the player was structurally present for its raw material."""
    item = _extractor_payload_to_item(
        payload={"content": "使用者說他最近在趕專案", "kind": "semantic"},
        character_id="c1",
        conversation_id="conv-1",
    )
    assert item is not None
    assert item.player_knowledge == PLAYER_KNOWLEDGE_SHARED


def test_llm_extractor_ignores_any_player_knowledge_the_model_emits() -> None:
    """Red line: the value is derived from the pathway, not the payload.
    A model that hallucinated the field must not be able to launder a
    turn memory into ``private``."""
    item = _extractor_payload_to_item(
        payload={"content": "x", "player_knowledge": "private"},
        character_id="c1",
        conversation_id="conv-1",
    )
    assert item is not None
    assert item.player_knowledge == PLAYER_KNOWLEDGE_SHARED


# ── station 2: post-turn processor ───────────────────────────────────


def test_post_turn_processor_stamps_shared() -> None:
    """Covers chat replies and every delivered-message path that reuses
    post-turn (proactive, busy-defer, scheduled promise): the player
    received the message this memory summarises."""
    item = _post_turn_payload_to_item(
        {"content": "他答應週末一起去看展", "kind": "episodic"},
        character_id="c1",
        conversation_id="conv-1",
    )
    assert item is not None
    assert item.player_knowledge == PLAYER_KNOWLEDGE_SHARED


def test_post_turn_processor_ignores_model_supplied_player_knowledge() -> None:
    item = _post_turn_payload_to_item(
        {"content": "x", "player_knowledge": "private"},
        character_id="c1",
        conversation_id="conv-1",
    )
    assert item is not None
    assert item.player_knowledge == PLAYER_KNOWLEDGE_SHARED


# ── stations 3+4: story_event_service (beat realize / autonomous) ─────


class _NullExpander(StoryEventExpanderPort):
    async def expand(
        self, *, seed, character_name, character_summary, speaking_style,
        world_frame, scene=None, character=None,
    ):
        return (f"展開：{seed.seed_text}", None)


class _ArcRealizingProcessor:
    """A post-turn pass whose only output is "that beat just happened"."""

    def __init__(self, *, beat_id: str, narrative: str) -> None:
        self._beat_id = beat_id
        self._narrative = narrative

    async def process(self, **_kwargs: Any) -> PostTurnResult:
        return PostTurnResult(
            arc_adjustments=[
                ArcAdjustmentSignal(
                    action="mark_realized",
                    beat_id=self._beat_id,
                    narrative=self._narrative,
                ),
            ],
        )


class _OnePositionedBeatPlanner(StoryArcPlannerPort):
    def __init__(self, today: date, *, position: str | None) -> None:
        self._today = today
        self._position = position

    async def plan_arc(
        self,
        *,
        character: Character,
        start_date: date,
        duration_days: int = 21,
        beat_count_hint: int = 5,
        hint: str | None = None,
        recent_dialogue_summary: str = "",
    ) -> StoryArc:
        arc = StoryArc.create(
            character_id=character.id,
            title="test arc",
            premise="setup premise",
            theme="custom",
            start_date=start_date,
            end_date=start_date + timedelta(days=duration_days),
        )
        beat = StoryArcBeat.create(
            arc_id=arc.id, sequence=0,
            scheduled_date=self._today,
            title="today beat", summary="今天要發生的事",
            tension=TENSION_SETUP,
            operator_position=self._position,
        )
        return arc.with_beats([beat])


def _story_services(today: date, *, position: str | None):
    arc_service = StoryArcService(
        repository=InMemoryStoryArcRepository(),
        planner=_OnePositionedBeatPlanner(today, position=position),
    )
    memory_repo = InMemoryMemoryRepository()
    event_repo = InMemoryStoryEventRepository()
    event_service = StoryEventService(
        gacha=StoryGachaService(
            seed_repository=InMemoryStorySeedRepository(),
            event_repository=event_repo,
        ),
        expander=_NullExpander(),
        event_repository=event_repo,
        memory_repository=memory_repo,
        embedder=None,
        local_tz=UTC,
        arc_service=arc_service,
    )
    return event_service, arc_service, memory_repo


def _positioned_beat(position: str | None) -> StoryArcBeat:
    return StoryArcBeat.create(
        arc_id="arc-1", sequence=0,
        scheduled_date=date(2026, 8, 26),
        title="today beat", summary="今天要發生的事",
        tension=TENSION_SETUP,
        operator_position=position,
    )


@pytest.mark.parametrize(
    ("player_present", "position", "expected"),
    [
        # F2: the player watched it land. The beat's plan — including
        # the ``None`` that covers essentially the whole existing
        # corpus — cannot overrule what actually happened in the room.
        (True, OPERATOR_POSITION_CENTRAL, PLAYER_KNOWLEDGE_SHARED),
        (True, OPERATOR_POSITION_PRESENT, PLAYER_KNOWLEDGE_SHARED),
        (True, OPERATOR_POSITION_ABSENT, PLAYER_KNOWLEDGE_SHARED),
        (True, None, PLAYER_KNOWLEDGE_SHARED),
        # Nobody in the room: the beat's own position is the only
        # evidence, and its unjudged ``None`` stays unjudged ("") rather
        # than becoming a fabricated "the player does not know".
        (False, OPERATOR_POSITION_CENTRAL, PLAYER_KNOWLEDGE_SHARED),
        (False, OPERATOR_POSITION_PRESENT, PLAYER_KNOWLEDGE_SHARED),
        (False, OPERATOR_POSITION_ABSENT, PLAYER_KNOWLEDGE_PRIVATE),
        (False, None, ""),
    ],
)
def test_story_memory_verdict_matrix(
    player_present: bool, position: str | None, expected: str,
) -> None:
    assert _player_knowledge_for_story_memory(
        player_present=player_present,
        beat=_positioned_beat(position),
    ) == expected


@pytest.mark.parametrize("player_present", [False, True])
def test_story_memory_without_a_beat_is_private_unless_played(
    player_present: bool,
) -> None:
    """No beat = the gacha day-roll (or a failed arc lookup). That
    station is *classified*, not unjudged — the world simulation wrote
    it unseen — so it stays ``private`` on the unattended side."""
    assert _player_knowledge_for_story_memory(
        player_present=player_present, beat=None,
    ) == (PLAYER_KNOWLEDGE_SHARED if player_present else PLAYER_KNOWLEDGE_PRIVATE)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("player_present", "position", "expected"),
    [
        (False, OPERATOR_POSITION_CENTRAL, PLAYER_KNOWLEDGE_SHARED),
        (False, OPERATOR_POSITION_PRESENT, PLAYER_KNOWLEDGE_SHARED),
        (False, OPERATOR_POSITION_ABSENT, PLAYER_KNOWLEDGE_PRIVATE),
        (False, None, ""),
        (True, OPERATOR_POSITION_CENTRAL, PLAYER_KNOWLEDGE_SHARED),
        (True, OPERATOR_POSITION_PRESENT, PLAYER_KNOWLEDGE_SHARED),
        (True, OPERATOR_POSITION_ABSENT, PLAYER_KNOWLEDGE_SHARED),
        (True, None, PLAYER_KNOWLEDGE_SHARED),
    ],
)
async def test_beat_realize_stamps_from_presence_then_position(
    player_present: bool, position: str | None, expected: str,
) -> None:
    today = date(2026, 8, 26)
    event_service, arc_service, memory_repo = _story_services(
        today, position=position,
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)

    await event_service.record_arc_beat_realization(
        character, beat_id=arc.beats[0].id, narrative="今天發生了那件事。",
        now=_NOW, player_present=player_present,
    )

    memories = await memory_repo.query(character.id)
    memory = next(m for m in memories if m.content == "今天發生了那件事。")
    assert memory.player_knowledge == expected


@pytest.mark.asyncio
async def test_beat_realize_defaults_to_the_position_projection() -> None:
    """A caller that forgets the flag must not silently claim presence:
    the default falls back to the beat's own (here unjudged) position."""
    today = date(2026, 8, 26)
    event_service, arc_service, memory_repo = _story_services(
        today, position=None,
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)

    await event_service.record_arc_beat_realization(
        character, beat_id=arc.beats[0].id, narrative="今天發生了那件事。",
        now=_NOW,
    )

    memories = await memory_repo.query(character.id)
    memory = next(m for m in memories if m.content == "今天發生了那件事。")
    assert memory.player_knowledge == ""


@pytest.mark.asyncio
async def test_autonomous_story_event_stamps_private() -> None:
    """No beat at all — the world simulation wrote this while the player
    was not looking, so it is not theirs to already know."""
    today = date(2026, 8, 26)
    event_service, _, memory_repo = _story_services(today, position=None)
    character = _character()

    event = await event_service._build_and_persist(  # noqa: SLF001
        character, today, StorySeed.create(seed_text="撿到一張會動的地圖"),
    )

    assert event is not None
    memories = await memory_repo.query(character.id)
    memory = next(m for m in memories if m.content == event.narrative)
    assert memory.player_knowledge == PLAYER_KNOWLEDGE_PRIVATE


@pytest.mark.asyncio
async def test_post_turn_mark_realized_is_shared_despite_an_unjudged_beat() -> None:
    """F2, from the caller's side and end to end: the player performed
    this beat in the turn that just finished, so the memory of it must be
    common ground even though the beat's ``operator_position`` is unset —
    the state of essentially every beat in the existing corpus. Getting
    this wrong is directly visible: the character re-introduces a scene
    the player acted out as if it were news."""
    today = date(2026, 8, 26)
    event_service, arc_service, memory_repo = _story_services(
        today, position=None,
    )
    characters = InMemoryCharacterRepository()
    conversations = InMemoryConversationRepository()
    character = _character()
    await characters.save(character)
    arc = await arc_service.start_new_arc(character, today=today)
    beat = arc.beats[0]

    registry = InMemoryChatModelRegistry(default_provider_id="fake")
    registry.register(FakeChatModel(provider_id="fake"))
    chat = ChatService(
        character_repository=characters,
        conversation_repository=conversations,
        memory_repository=memory_repo,
        post_turn_processor=_ArcRealizingProcessor(
            beat_id=beat.id, narrative="我們一起把那句話說完了。",
        ),
        prompt_context_builder=DefaultPromptContextBuilder(),
        model_registry=registry,
        state_engine=SimpleStateEngine(),
        extract_in_background=False,
        story_event_service=event_service,
        story_arc_service=arc_service,
    )
    await conversations.save(Conversation(
        id="conv-1", character_id=character.id, messages=[
            Message(
                role=MessageRole.USER, content="那句話你到底想說什麼",
                created_at=_NOW - timedelta(minutes=2),
            ),
            Message(
                role=MessageRole.ASSISTANT, content="……我說了。",
                created_at=_NOW - timedelta(minutes=1),
            ),
        ],
    ))

    await chat._do_post_turn(  # noqa: SLF001
        character=character,
        conversation_id="conv-1",
        turn_record_id="turn-1",
        user_text="那句話你到底想說什麼",
        assistant_text="……我說了。",
        prior_messages=[],
    )

    memories = await memory_repo.query(character.id)
    memory = next(
        m for m in memories if m.content == "我們一起把那句話說完了。"
    )
    assert memory.player_knowledge == PLAYER_KNOWLEDGE_SHARED


# ── station 5: schedule_memorializer ─────────────────────────────────

_DAY = date(2026, 8, 26)
_ACT_START = datetime(2026, 8, 26, 14, 0, tzinfo=UTC)
_ACT_END = datetime(2026, 8, 26, 15, 0, tzinfo=UTC)
_AFTER = datetime(2026, 8, 26, 18, 0, tzinfo=UTC)


def _activity(*, role: str | None) -> ScheduleActivity:
    refs = (
        (
            ParticipantRef(
                actor_kind="operator", actor_id=None,
                display_name="木木", role=role,
            ),
        )
        if role
        else ()
    )
    return ScheduleActivity.create(
        start_at=_ACT_START,
        end_at=_ACT_END,
        description="去刨冰店吃刨冰",
        category="social",
        location="刨冰店",
        participant_refs=refs,
    )


class _CannedAftermath:
    """Supplies the honest ``factual_summary`` CF4 requires before an
    operator-involved-but-unverified activity may be memorialised at
    all. Without it that case writes nothing, and a station that writes
    nothing cannot demonstrate what it stamps."""

    def __init__(self, factual_summary: str) -> None:
        self._answer = ActivityAftermath(factual_summary=factual_summary)

    async def judge(
        self, *, character, activity,  # noqa: ANN001
        operator_primary_language: str = "zh-TW",
        evidence=None,  # noqa: ANN001
    ) -> ActivityAftermath:
        del character, activity, operator_primary_language, evidence
        return self._answer


async def _memorialize_activity(
    *,
    role: str | None,
    last_active_at: datetime | None,
    aftermath_port: _CannedAftermath | None = None,
) -> list[MemoryItem]:
    schedules = InMemoryScheduleRepository()
    memories = InMemoryMemoryRepository()
    characters = InMemoryCharacterRepository()
    character = _character(name="芊璃", last_active_at=last_active_at)
    await characters.save(character)
    await schedules.save(
        DailySchedule.create(
            character_id=character.id, date_=_DAY,
            activities=[_activity(role=role)],
        ),
    )
    await ScheduleMemorializer(
        schedule_repository=schedules,
        memory_repository=memories,
        local_tz=UTC,
        character_repository=characters,
        aftermath_port=aftermath_port,
    ).memorialize(character_id=character.id, now=_AFTER)
    return await memories.query(character.id, limit=10)


@pytest.mark.asyncio
async def test_solo_activity_memorialized_as_private() -> None:
    written = await _memorialize_activity(role=None, last_active_at=None)
    assert len(written) == 1
    assert written[0].player_knowledge == PLAYER_KNOWLEDGE_PRIVATE


@pytest.mark.asyncio
async def test_verified_shared_activity_memorialized_as_shared() -> None:
    """The one path to ``shared``: the CF4 evidence chain returned
    VERIFIED (operator agreed *and* interacted while the block ran)."""
    written = await _memorialize_activity(
        role=OPERATOR_CONFIRMED_SHARED_ROLE,
        last_active_at=datetime(2026, 8, 26, 14, 30, tzinfo=UTC),
    )
    assert len(written) == 1
    assert written[0].player_knowledge == PLAYER_KNOWLEDGE_SHARED


@pytest.mark.asyncio
async def test_agreed_but_unverified_activity_stays_private() -> None:
    """Agreement is not attendance. Absence of evidence must not read as
    presence — the incident this plan exists to stop."""
    written = await _memorialize_activity(
        role=OPERATOR_CONFIRMED_SHARED_ROLE,
        last_active_at=datetime(2026, 8, 25, 21, 0, tzinfo=UTC),
        aftermath_port=_CannedAftermath("本來想約他一起，最後自己去了"),
    )
    assert len(written) == 1
    assert written[0].player_knowledge == PLAYER_KNOWLEDGE_PRIVATE


@pytest.mark.asyncio
async def test_pending_invite_writes_nothing_so_nothing_is_stamped() -> None:
    """CF4's older guard still holds ahead of this field: a never-agreed
    invite produces no memory at all, so there is no row to mis-stamp."""
    written = await _memorialize_activity(
        role=OPERATOR_INVITE_PENDING_ROLE, last_active_at=None,
    )
    assert written == []


# ── station 6: story_scene_closing ───────────────────────────────────


@pytest.mark.asyncio
async def test_scene_side_story_memory_is_shared() -> None:
    """A scene session only exists because the player played it — even
    though ``audience`` is deliberately left unjudged here, the
    player-knowledge verdict is not a judgement call."""
    memories = InMemoryMemoryRepository()
    coordinator = SceneClosingCoordinator(
        sessions=MagicMock(),
        conversations=MagicMock(),
        closer=None,
        memory_repository=memories,
    )
    session = StorySceneSession(
        id="scene-1", character_id="c1", conversation_id="conv-1",
        source_layer=SCENE_LAYER_SIDE_STORY,
    )

    wrote = await coordinator._remember_side_story(  # noqa: SLF001
        session=session, narrative="兩人在頂樓看完了整場煙火。",
        now=_NOW, degraded=False,
    )

    assert wrote is True
    written = await memories.query("c1", limit=10)
    assert len(written) == 1
    assert written[0].player_knowledge == PLAYER_KNOWLEDGE_SHARED
    # The two axes are independent: audience stays unjudged (KB5/§3.4 #4).
    assert written[0].audience == ""


@pytest.mark.asyncio
async def test_scene_close_beat_canon_agrees_with_its_own_side_story() -> None:
    """F2: layers 1/2 close through ``record_arc_beat_realization``, so
    they used to be judged by the beat's ``operator_position`` — ``None``
    on essentially the whole corpus — while layer 3 of the *same* close
    stamped ``shared``. One scene session cannot be two answers about
    whether the player was in the room."""
    today = date(2026, 8, 26)
    event_service, arc_service, memory_repo = _story_services(
        today, position=None,
    )
    character = _character()
    arc = await arc_service.start_new_arc(character, today=today)
    coordinator = SceneClosingCoordinator(
        sessions=MagicMock(),
        conversations=MagicMock(),
        closer=None,
        story_event_service=event_service,
    )
    session = StorySceneSession(
        id="scene-2", character_id=character.id, conversation_id="conv-1",
        source_layer=SCENE_LAYER_SIDE_STORY, beat_id=arc.beats[0].id,
    )

    landed = await coordinator._realize_beat(  # noqa: SLF001
        character, session=session,
        narrative="兩人在頂樓看完了整場煙火。", tone=None, now=_NOW,
    )

    assert landed is True
    written = await memory_repo.query(character.id, limit=10)
    memory = next(
        m for m in written if m.content == "兩人在頂樓看完了整場煙火。"
    )
    assert memory.player_knowledge == PLAYER_KNOWLEDGE_SHARED


# ── station 7: relationship_milestone_service ────────────────────────


@pytest.mark.asyncio
async def test_relationship_milestone_is_shared_though_audience_is_private() -> None:
    """The sharpest divergence between the two axes: never broadcastable,
    yet entirely built out of the player's own messages."""
    persona_service = MagicMock()
    persona_service.get_interaction_strength = AsyncMock(
        return_value=InteractionStrength(
            character_id="c1",
            operator_id="default",
            first_message_at=datetime(2026, 8, 1, tzinfo=UTC),
            total_user_messages=12,
            days_since_first_contact=25,
            messages_last_7_days=5,
            messages_last_30_days=12,
            longest_session_minutes=40,
            shared_arc_realized_count=0,
            shared_drama_count=0,
            familiarity_band=Familiarity.ACQUAINTANCE,
            computed_at=_NOW,
        ),
    )
    memory_repo = AsyncMock()
    memory_repo.query = AsyncMock(return_value=[])
    memory_repo.add = AsyncMock(side_effect=lambda item: item)

    emitted = await RelationshipMilestoneService(
        persona_service=persona_service,
        memory_repository=memory_repo,
        settings=HumanizationSettings(),
    ).check_and_emit("c1", "default", now=_NOW)

    assert emitted is not None
    assert emitted.player_knowledge == PLAYER_KNOWLEDGE_SHARED
    assert emitted.audience == "private"


# ── station 8: feed_reaction_memorializer ────────────────────────────


@pytest.mark.asyncio
async def test_feed_reaction_memory_is_shared() -> None:
    """The memory *is* the player's own action; they cannot be unaware
    of a like they pressed."""
    posts = InMemoryFeedPostRepository()
    reactions = InMemoryFeedReactionRepository()
    memories = InMemoryMemoryRepository()
    post = FeedPost.create(
        character_id="c1", kind=FeedKind.MOOD, content_text="今天去咖啡廳寫稿",
        source=FeedSource.silence(),
        created_at=datetime(2026, 8, 26, 9, 0, tzinfo=UTC),
    )
    await posts.add(post)
    await reactions.add(
        FeedReaction.create(
            post_id=post.id,
            created_at=datetime(2026, 8, 26, 10, 0, tzinfo=UTC),
        ),
    )

    await FeedReactionMemorializer(
        post_repository=posts,
        reaction_repository=reactions,
        comment_repository=InMemoryFeedCommentRepository(),
        memory_repository=memories,
    ).memorialize(character_id="c1", now=_NOW)

    written = await memories.query("c1", limit=10)
    assert len(written) == 1
    assert written[0].player_knowledge == PLAYER_KNOWLEDGE_SHARED


# ── station 9: feed_comment_reply_service ────────────────────────────


def test_feed_comment_reply_memory_is_shared() -> None:
    post = FeedPost.create(
        character_id="c1", kind=FeedKind.MOOD, content_text="今天試了新的拉花",
        source=FeedSource.silence(), created_at=_NOW,
    )
    user_comment = FeedComment.create(
        post_id=post.id, content_text="好厲害", created_at=_NOW,
    )
    reply = FeedComment.create(
        post_id=post.id, content_text="謝謝你～", author_id="c1", created_at=_NOW,
    )

    memory = _build_reply_memory(
        character_id="c1", post=post,
        user_comments=[user_comment], reply=reply,
    )

    assert memory.player_knowledge == PLAYER_KNOWLEDGE_SHARED


# ── station 10: feed_composer_service ────────────────────────────────


def test_feed_self_post_memory_is_unjudged() -> None:
    """F3b: ``""``, not ``disclosed``. Posting is not reading — this
    station only knows the character published, never whether the
    player actually saw the post, and there is no view-gate linking
    this memory back to the post id to promote it once they do."""
    post = FeedPost.create(
        character_id="c1", kind=FeedKind.MOOD,
        content_text="一個人去了河堤，風很大",
        source=FeedSource.memory("mem-42"), created_at=_NOW,
    )

    memory = _post_to_memory(post)

    assert memory.player_knowledge == ""


# ── station 11: character_encounter_service ──────────────────────────


@pytest.mark.asyncio
async def test_encounter_summary_hearsay_and_peer_facts_are_all_private() -> None:
    """Character-to-character, off-screen. Nothing an encounter produces
    is common ground with the player."""
    repository = InMemoryMemoryRepository()
    char_a = SimpleNamespace(id="a", name="A")
    char_b = SimpleNamespace(id="b", name="B")

    await CharacterEncounterMemoryWriter(repository=repository).write(
        encounter=SimpleNamespace(
            id="enc-1", relationship_id="rel-1", location="神社前庭",
            trigger_reason="路過", max_turns=2, scheduled_for=_NOW,
        ),
        char_a=char_a, char_b=char_b, transcript=(),
        reflection=EncounterReflection(
            summary_for_a="A 和 B 在神社前庭聊了很久",
            summary_for_b="B 和 A 在神社前庭聊了很久",
            hearsay_for_a=(
                ReflectionMemoryEntry(content="B 說主人最近在趕專案"),
            ),
            peer_facts_for_a=(
                ReflectionMemoryEntry(content="B 常去河堤拍照"),
            ),
        ),
    )

    stored = await repository.list_all_for_character("a", world_scope=None)
    by_kind = {item.kind: item for item in stored}
    assert by_kind[MemoryKind.EPISODIC].player_knowledge == PLAYER_KNOWLEDGE_PRIVATE
    assert by_kind[MemoryKind.HEARSAY].player_knowledge == PLAYER_KNOWLEDGE_PRIVATE
    assert (
        by_kind[MemoryKind.RELATIONSHIP].player_knowledge
        == PLAYER_KNOWLEDGE_PRIVATE
    )


# ── station 12: character_social_knowledge_service ───────────────────


def test_peer_knowledge_seed_memory_is_private() -> None:
    memory = _seed_memory(
        character_id="c1", peer_character_id="c2", peer_name="小英",
        seed=PeerKnowledgeSeed(summary="小英是隔壁花店的老闆"),
        now=_NOW,
    )
    assert memory.player_knowledge == PLAYER_KNOWLEDGE_PRIVATE


# ── not a station: consolidation propagates, never mints ─────────────


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        # 1. any private wins, against every other value including the
        #    unjudged "" — "" is rendering-equivalent to shared (no
        #    frame), so letting it win would strip the boundary.
        ([PLAYER_KNOWLEDGE_PRIVATE, ""], PLAYER_KNOWLEDGE_PRIVATE),
        ([PLAYER_KNOWLEDGE_SHARED, PLAYER_KNOWLEDGE_PRIVATE],
         PLAYER_KNOWLEDGE_PRIVATE),
        ([PLAYER_KNOWLEDGE_DISCLOSED, PLAYER_KNOWLEDGE_PRIVATE],
         PLAYER_KNOWLEDGE_PRIVATE),
        # 2. else any unjudged wins — a merge must not invent a verdict
        #    for material no station judged.
        ([PLAYER_KNOWLEDGE_SHARED, ""], ""),
        ([PLAYER_KNOWLEDGE_DISCLOSED, ""], ""),
        # 3. else any disclosed wins.
        ([PLAYER_KNOWLEDGE_SHARED, PLAYER_KNOWLEDGE_DISCLOSED],
         PLAYER_KNOWLEDGE_DISCLOSED),
        # 4. else shared.
        ([PLAYER_KNOWLEDGE_SHARED, PLAYER_KNOWLEDGE_SHARED],
         PLAYER_KNOWLEDGE_SHARED),
        # Degenerate input: no sources means no verdict.
        ([], ""),
    ],
)
def test_merge_player_knowledge_rule(sources: list[str], expected: str) -> None:
    assert merge_player_knowledge(sources) == expected


def test_merge_player_knowledge_always_keeps_private() -> None:
    """Direction lock, stated independently of the table above.

    Asserted as ``== private``, not ``!= shared``: ``""`` is not shared
    but renders identically to it (``memory_knowledge_frame`` maps both
    to no frame), so a ``!= shared`` assertion would pass on exactly the
    outcome it is supposed to forbid.
    """
    for other in ("", PLAYER_KNOWLEDGE_SHARED, PLAYER_KNOWLEDGE_DISCLOSED):
        assert (
            merge_player_knowledge([PLAYER_KNOWLEDGE_PRIVATE, other])
            == PLAYER_KNOWLEDGE_PRIVATE
        )
        assert (
            merge_player_knowledge([other, PLAYER_KNOWLEDGE_PRIVATE])
            == PLAYER_KNOWLEDGE_PRIVATE
        )


def test_merge_of_private_and_unjudged_still_renders_the_unaware_frame() -> None:
    """The lock restated at the surface it actually protects: after a
    merge with a legacy (``""``) sibling, recall must still tell the
    model the player was not there."""
    item = MemoryItem.create(
        character_id="c1",
        kind=MemoryKind.EPISODIC,
        content="她一個人去了山區",
        player_knowledge=merge_player_knowledge([PLAYER_KNOWLEDGE_PRIVATE, ""]),
    )
    assert memory_knowledge_frame(item) == PLAYER_UNAWARE_FRAME


class _EchoConsolidator:
    async def merge(  # noqa: ANN001
        self, cluster, *, character=None, operator_primary_language="zh-TW",
    ):
        del character, operator_primary_language
        return MergeProposal(
            content="MERGED: " + " / ".join(c.content for c in cluster),
            kind=cluster[0].kind,
            salience=max(c.salience for c in cluster),
            tags=(),
        )


class _StubEmbedder:
    @property
    def dimension(self) -> int:
        return 3

    @property
    def is_operational(self) -> bool:
        return True

    async def embed(self, text: str):
        raise NotImplementedError

    async def embed_many(self, texts: Any) -> list[tuple[float, ...] | None]:
        return [(float(len(t)), 0.0, 0.0) for t in texts]


def _clustered_item(content: str, player_knowledge: str) -> MemoryItem:
    return MemoryItem(
        id=str(uuid4()),
        character_id="c1",
        conversation_id=None,
        kind=MemoryKind.SEMANTIC,
        content=content,
        salience=0.7,
        created_at=_NOW,
        embedding=(1.0, 0.0, 0.0),
        player_knowledge=player_knowledge,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sibling",
    [
        PLAYER_KNOWLEDGE_SHARED,
        # The realistic case: the whole pre-KB5 back catalogue is "",
        # so almost every early merge pairs a fresh private row with an
        # unjudged one. This is the combination that used to launder.
        "",
    ],
)
async def test_consolidation_merge_inherits_the_conservative_verdict(
    sibling: str,
) -> None:
    """End-to-end through the service: a private member drags the merged
    row to ``private`` whatever the other member carried."""
    repo = InMemoryMemoryRepository()
    await repo.add(_clustered_item("她一個人去了河堤", PLAYER_KNOWLEDGE_PRIVATE))
    await repo.add(_clustered_item("她一個人去了河堤散步", sibling))

    report = await MemoryConsolidationService(
        memory_repository=repo,
        consolidator=_EchoConsolidator(),
        embedder=_StubEmbedder(),
    ).consolidate("c1")

    assert report.clusters_merged == 1
    remaining = await repo.list_all_for_character("c1")
    assert len(remaining) == 1
    assert remaining[0].player_knowledge == PLAYER_KNOWLEDGE_PRIVATE
