"""Integration tests for SQLAlchemy repository adapters against PostgreSQL.

The ``engine`` / ``session_factory`` fixtures come from
``tests/conftest.py`` and run against a testcontainers-managed Postgres
instance (pgvector/pgvector:pg16). If Docker isn't available the whole
module is skipped — unit tests still provide the bulk of coverage.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker  # noqa: F401 — used as type hint in tests

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageContentMode,
    MessageRole,
)
from kokoro_link.domain.entities.character_encounter import CharacterEncounter, EncounterLine
from kokoro_link.domain.entities.character_encounter_intent import (
    CharacterEncounterIntent,
)
from kokoro_link.domain.entities.character_peer_profile import CharacterPeerProfile
from kokoro_link.domain.entities.character_relationship import CharacterRelationship
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)
from kokoro_link.domain.value_objects.delivery_mode import DeliveryMode
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.infrastructure.persistence.sa_character_repository import SACharacterRepository
from kokoro_link.infrastructure.persistence.sa_character_encounter_repository import (
    SACharacterEncounterRepository,
)
from kokoro_link.infrastructure.persistence.sa_character_encounter_intent_repository import (
    SACharacterEncounterIntentRepository,
)
from kokoro_link.infrastructure.persistence.sa_character_peer_profile_repository import (
    SACharacterPeerProfileRepository,
)
from kokoro_link.infrastructure.persistence.sa_character_relationship_repository import (
    SACharacterRelationshipRepository,
)
from kokoro_link.domain.entities.operator_profile import OperatorProfile
from kokoro_link.infrastructure.persistence.sa_operator_profile_repository import (
    SAOperatorProfileRepository,
)
from kokoro_link.infrastructure.persistence.sa_conversation_repository import SAConversationRepository
from kokoro_link.infrastructure.persistence.sa_memory_repository import SAMemoryRepository
from kokoro_link.infrastructure.persistence.sa_messaging_account_repository import (
    SAMessagingAccountRepository,
)
from kokoro_link.infrastructure.persistence.sa_schedule_repository import SAScheduleRepository


# ---------- Character ----------


@pytest.mark.asyncio
async def test_character_relationship_and_encounter_round_trip(
    session_factory: sessionmaker,
) -> None:
    relationship_repo = SACharacterRelationshipRepository(session_factory)
    encounter_repo = SACharacterEncounterRepository(session_factory)
    relationship = CharacterRelationship.create(
        character_a_id="char-a",
        character_b_id="char-b",
        relationship_label="朋友",
        how_a_sees_b="A 覺得 B 可靠",
        how_b_sees_a="B 覺得 A 親切",
    )
    await relationship_repo.save(relationship)

    loaded = await relationship_repo.get_pair("char-b", "char-a")
    assert loaded is not None
    assert loaded.id == relationship.id
    assert loaded.relationship_label == "朋友"

    encounter = CharacterEncounter.plan(
        relationship_id=relationship.id,
        character_a_id=relationship.character_a_id,
        character_b_id=relationship.character_b_id,
        scheduled_for=datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc),
        location="咖啡廳",
        trigger_reason="自然碰面",
    ).complete(
        transcript=(
            EncounterLine(speaker_character_id="char-a", text="早安。"),
            EncounterLine(speaker_character_id="char-b", text="早安。"),
        ),
        summary_for_a="A 和 B 見面。",
        summary_for_b="B 和 A 見面。",
        memory_ids=("mem-a", "mem-b"),
        at=datetime(2026, 5, 17, 9, 5, tzinfo=timezone.utc),
    )
    await encounter_repo.save(encounter)

    encounters = await encounter_repo.list_for_character("char-a")
    assert encounters[0].status == "completed"
    assert encounters[0].transcript[0].speaker_character_id == "char-a"
    assert encounters[0].memory_ids == ("mem-a", "mem-b")


@pytest.mark.asyncio
async def test_character_peer_profile_round_trip(session_factory: sessionmaker) -> None:
    repo = SACharacterPeerProfileRepository(session_factory)
    now = datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)
    profile = CharacterPeerProfile.create(
        character_id="char-a",
        peer_character_id="char-b",
        peer_name="Mio",
        summary="Often visits the station cafe.",
        occupation="barista",
        haunts=("station cafe",),
        habits=("checks in after work",),
        relationship_note="Friendly regular.",
        confidence=0.8,
        last_consolidated_at=now,
        last_seen_at=now,
        source_memory_ids=("mem-1", "mem-2"),
    )

    await repo.save(profile)
    loaded = await repo.get("char-a", "char-b")

    assert loaded is not None
    assert loaded.id == profile.id
    assert loaded.peer_name == "Mio"
    assert loaded.haunts == ("station cafe",)
    assert loaded.habits == ("checks in after work",)
    assert loaded.source_memory_ids == ("mem-1", "mem-2")

    listed = await repo.list_for_character("char-a")
    assert [item.peer_character_id for item in listed] == ["char-b"]


@pytest.mark.asyncio
async def test_character_encounter_intent_round_trip(
    session_factory: sessionmaker,
) -> None:
    repo = SACharacterEncounterIntentRepository(session_factory)
    now = datetime(2026, 5, 17, 9, 0, tzinfo=timezone.utc)
    intent = CharacterEncounterIntent.create(
        character_id="char-a",
        peer_character_id="char-b",
        desired_after=now + timedelta(days=1),
        topic="聊使用者交代的明天碰面",
        source_text="明天去找小鈴",
        now=now,
    )

    await repo.add(intent)
    pending = await repo.find_pending_for_pair(
        "char-b",
        "char-a",
        now=now,
        horizon=now + timedelta(days=2),
    )
    assert pending is not None
    assert pending.id == intent.id
    assert pending.topic == "聊使用者交代的明天碰面"

    await repo.save(pending.mark_consumed(at=now + timedelta(hours=1)))
    assert await repo.find_pending_for_pair(
        "char-a",
        "char-b",
        now=now,
        horizon=now + timedelta(days=2),
    ) is None

    await repo.delete_for_character("char-a")
    assert await repo.get(intent.id) is None


@pytest.mark.asyncio
async def test_character_save_and_get(session_factory: sessionmaker) -> None:
    repo = SACharacterRepository(session_factory)
    character = Character.create(
        name="Airi",
        summary="溫柔的角色",
        personality=["gentle", "kind"],
        interests=["music"],
        speaking_style="soft",
        boundaries=["no violence"],
        gender_identity="女性",
        third_person_pronoun="她",
        visual_gender_presentation="feminine woman",
        visual_subject_type="human",
        visual_generation_style="realistic",
        personality_type=CharacterPersonalityType(
            code="ISFJ",
            source="llm_inferred",
            confidence=0.72,
            rationale="照顧他人且重視穩定日常。",
        ),
        state=CharacterState(emotion="happy", affection=50, fatigue=0, trust=50, energy=100),
    )

    await repo.save(character)
    loaded = await repo.get(character.id)

    assert loaded is not None
    assert loaded.id == character.id
    assert loaded.name == "Airi"
    assert loaded.personality == ["gentle", "kind"]
    assert loaded.interests == ["music"]
    assert loaded.gender_identity == "女性"
    assert loaded.third_person_pronoun == "她"
    assert loaded.visual_gender_presentation == "feminine woman"
    assert loaded.visual_subject_type == "human"
    assert loaded.visual_generation_style == "realistic"
    assert loaded.personality_type.code == "ISFJ"
    assert loaded.personality_type.rationale == "照顧他人且重視穩定日常。"
    assert loaded.state.emotion == "happy"
    assert loaded.state.energy == 100


@pytest.mark.asyncio
async def test_character_update(session_factory: sessionmaker) -> None:
    repo = SACharacterRepository(session_factory)
    character = Character.create(
        name="Mio",
        summary="活潑的角色",
        personality=["cheerful"],
        interests=["games"],
        speaking_style="energetic",
        boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )
    await repo.save(character)

    updated = character.update(name="Mio v2", summary=None, personality=None, interests=["games", "anime"], speaking_style=None, boundaries=None, state=None)
    await repo.save(updated)

    loaded = await repo.get(character.id)
    assert loaded is not None
    assert loaded.name == "Mio v2"
    assert loaded.interests == ["games", "anime"]


@pytest.mark.asyncio
async def test_character_stale_save_preserves_dedicated_locks(
    session_factory: sessionmaker,
) -> None:
    repo = SACharacterRepository(session_factory)
    stale = Character.create(
        name="Mio", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=0, trust=50, energy=100,
        ),
    )
    await repo.save(stale)
    frozen_at = datetime(2026, 7, 10, tzinfo=timezone.utc)
    await repo.set_frozen(
        stale.id, frozen=True, now=frozen_at, reason="manual",
    )
    await repo.set_subscription_locked(stale.id, locked=True)

    await repo.save(replace(stale, name="Mio v2"))

    loaded = await repo.get(stale.id)
    assert loaded is not None
    assert loaded.name == "Mio v2"
    assert loaded.frozen is True
    assert loaded.frozen_at == frozen_at
    assert loaded.frozen_reason == "manual"
    assert loaded.subscription_locked is True


@pytest.mark.asyncio
async def test_character_list(session_factory: sessionmaker) -> None:
    repo = SACharacterRepository(session_factory)
    c1 = Character.create(name="A", summary="", personality=[], interests=[], speaking_style="", boundaries=[], state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100))
    c2 = Character.create(name="B", summary="", personality=[], interests=[], speaking_style="", boundaries=[], state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100))
    await repo.save(c1)
    await repo.save(c2)

    result = await repo.list()
    assert len(result) == 2
    names = {c.name for c in result}
    assert names == {"A", "B"}


@pytest.mark.asyncio
async def test_character_get_nonexistent(session_factory: sessionmaker) -> None:
    repo = SACharacterRepository(session_factory)
    assert await repo.get("nonexistent") is None


@pytest.mark.asyncio
async def test_messaging_account_polling_lock_is_exclusive(
    session_factory: sessionmaker,
) -> None:
    char_repo = SACharacterRepository(session_factory)
    account_repo = SAMessagingAccountRepository(session_factory)
    character = Character.create(
        name="Mio",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )
    await char_repo.save(character)
    account = MessagingAccount.create(
        character_id=character.id,
        platform=Platform.TELEGRAM,
        credentials={"bot_token": "TOKEN"},
        delivery_mode=DeliveryMode.POLLING,
    )
    await account_repo.save(account)

    now = datetime(2026, 5, 31, 12, 0, tzinfo=timezone.utc)
    first = await account_repo.try_acquire_polling_lock(
        account.id,
        owner_id="worker-a",
        now=now,
        ttl=timedelta(seconds=60),
    )
    second = await account_repo.try_acquire_polling_lock(
        account.id,
        owner_id="worker-b",
        now=now + timedelta(seconds=1),
        ttl=timedelta(seconds=60),
    )

    assert first is not None
    assert second is None

    assert await account_repo.release_polling_lock(
        account.id, owner_id="worker-a",
    )
    third = await account_repo.try_acquire_polling_lock(
        account.id,
        owner_id="worker-b",
        now=now + timedelta(seconds=2),
        ttl=timedelta(seconds=60),
    )
    assert third is not None
    assert third.polling_lock_owner == "worker-b"


@pytest.mark.asyncio
async def test_character_state_last_active_at_round_trips_as_utc_aware(
    session_factory: sessionmaker,
) -> None:
    """SQLite drops tzinfo on read; repo must re-attach UTC so downstream
    code (e.g. rest-recovery) can subtract ``datetime.now(timezone.utc)``.
    """
    repo = SACharacterRepository(session_factory)
    active_at = datetime(2026, 4, 17, 10, 0, tzinfo=timezone.utc)
    character = Character.create(
        name="TZTest",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="neutral", affection=50, fatigue=20, trust=50, energy=80,
            last_active_at=active_at,
        ),
    )
    await repo.save(character)
    loaded = await repo.get(character.id)

    assert loaded is not None
    assert loaded.state.last_active_at is not None
    assert loaded.state.last_active_at.tzinfo is not None
    # Should still subtract cleanly against an aware datetime.
    _ = (datetime.now(timezone.utc) - loaded.state.last_active_at).total_seconds()


# ---------- Conversation ----------


@pytest.mark.asyncio
async def test_conversation_save_and_get(session_factory: sessionmaker) -> None:
    repo = SAConversationRepository(session_factory)
    conv = Conversation.start(character_id="char-1")
    conv = conv.append(Message(role=MessageRole.USER, content="你好"))
    conv = conv.append(Message(role=MessageRole.ASSISTANT, content="你好呀！"))

    await repo.save(conv)
    loaded = await repo.get(conv.id)

    assert loaded is not None
    assert loaded.id == conv.id
    assert loaded.character_id == "char-1"
    assert len(loaded.messages) == 2
    assert loaded.messages[0].role == MessageRole.USER
    assert loaded.messages[0].content == "你好"
    assert loaded.messages[1].role == MessageRole.ASSISTANT


@pytest.mark.asyncio
async def test_conversation_round_trips_message_content_mode_and_safe_summary(
    session_factory: sessionmaker,
) -> None:
    repo = SAConversationRepository(session_factory)
    conv = Conversation.start(character_id="char-safe-summary")
    conv = conv.append(
        Message(
            role=MessageRole.ASSISTANT,
            content="raw restricted text",
            content_mode=MessageContentMode.NSFW,
            safe_summary="safe emotional summary",
        ),
    )

    await repo.save(conv)
    loaded = await repo.get(conv.id)

    assert loaded is not None
    assert loaded.messages[0].content_mode == MessageContentMode.NSFW
    assert loaded.messages[0].safe_summary == "safe emotional summary"


@pytest.mark.asyncio
async def test_conversation_append_messages(session_factory: sessionmaker) -> None:
    repo = SAConversationRepository(session_factory)
    conv = Conversation.start(character_id="char-1")
    await repo.save(conv)

    conv = conv.append(Message(role=MessageRole.USER, content="第一句"))
    conv = conv.append(Message(role=MessageRole.ASSISTANT, content="回覆"))
    await repo.save(conv)

    conv = conv.append(Message(role=MessageRole.USER, content="第二句"))
    await repo.save(conv)

    loaded = await repo.get(conv.id)
    assert loaded is not None
    assert len(loaded.messages) == 3
    assert loaded.messages[2].content == "第二句"


@pytest.mark.asyncio
async def test_conversation_get_nonexistent(session_factory: sessionmaker) -> None:
    repo = SAConversationRepository(session_factory)
    assert await repo.get("nonexistent") is None


@pytest.mark.asyncio
async def test_conversation_latest_for_character_picks_most_recent_activity(
    session_factory: sessionmaker,
) -> None:
    repo = SAConversationRepository(session_factory)

    older = Conversation.start(character_id="char-A")
    older = older.append(Message(role=MessageRole.USER, content="老對話"))
    await repo.save(older)

    newer = Conversation.start(character_id="char-A")
    newer = newer.append(Message(role=MessageRole.USER, content="新對話"))
    newer = newer.append(Message(role=MessageRole.ASSISTANT, content="回覆"))
    await repo.save(newer)

    other_char = Conversation.start(character_id="char-B")
    other_char = other_char.append(Message(role=MessageRole.USER, content="別人的"))
    await repo.save(other_char)

    latest = await repo.latest_for_character("char-A")
    assert latest is not None
    assert latest.id == newer.id
    assert len(latest.messages) == 2


@pytest.mark.asyncio
async def test_conversation_latest_for_character_returns_none_when_empty(
    session_factory: sessionmaker,
) -> None:
    repo = SAConversationRepository(session_factory)
    assert await repo.latest_for_character("nobody") is None


@pytest.mark.asyncio
async def test_recent_messages_for_character_merges_across_sources(
    session_factory: sessionmaker,
) -> None:
    """Cross-source merge guard against per-channel persona drift.

    The character is one person on web / telegram / line — the prompt
    history hand-off needs to interleave by ``created_at`` regardless
    of which conversation the message lives in.
    """
    from datetime import datetime, timedelta, timezone

    repo = SAConversationRepository(session_factory)

    base = datetime(2026, 5, 19, 12, 0, 0, tzinfo=timezone.utc)
    web = Conversation.start(character_id="char-A", source="web")
    web = web.append(Message(
        role=MessageRole.USER, content="web-早", created_at=base,
    ))
    web = web.append(Message(
        role=MessageRole.ASSISTANT,
        content="web-早回",
        created_at=base + timedelta(minutes=1),
    ))
    await repo.save(web)

    tg = Conversation.start(character_id="char-A", source="telegram")
    tg = tg.append(Message(
        role=MessageRole.USER,
        content="tg-中午",
        created_at=base + timedelta(minutes=30),
    ))
    await repo.save(tg)

    line = Conversation.start(character_id="char-A", source="line")
    line = line.append(Message(
        role=MessageRole.USER,
        content="line-下午",
        created_at=base + timedelta(minutes=120),
    ))
    await repo.save(line)

    merged = await repo.recent_messages_for_character("char-A", limit=10)
    assert [m.content for m in merged] == [
        "web-早", "web-早回", "tg-中午", "line-下午",
    ]

    tail = await repo.recent_messages_for_character("char-A", limit=2)
    assert [m.content for m in tail] == ["tg-中午", "line-下午"]


# ---------- Memory ----------


def _memory(
    *,
    character_id: str,
    kind: MemoryKind,
    content: str,
    salience: float = 0.5,
    tags: tuple[str, ...] = (),
    created_at: datetime | None = None,
) -> MemoryItem:
    return MemoryItem.create(
        character_id=character_id,
        conversation_id="conv-1",
        kind=kind,
        content=content,
        salience=salience,
        tags=tags,
        created_at=created_at,
    )


@pytest.mark.asyncio
async def test_memory_add_and_query_orders_newest_first(session_factory: sessionmaker) -> None:
    repo = SAMemoryRepository(session_factory)
    base = datetime.now(timezone.utc)

    await repo.add(_memory(character_id="char-1", kind=MemoryKind.SEMANTIC, content="first", created_at=base - timedelta(hours=3)))
    await repo.add(_memory(character_id="char-1", kind=MemoryKind.SEMANTIC, content="second", created_at=base - timedelta(hours=2)))
    await repo.add(_memory(character_id="char-1", kind=MemoryKind.SEMANTIC, content="third", created_at=base - timedelta(hours=1)))

    result = await repo.query("char-1", limit=2)
    assert [item.content for item in result] == ["third", "second"]


@pytest.mark.asyncio
async def test_memory_query_filters_by_kind_and_salience(session_factory: sessionmaker) -> None:
    repo = SAMemoryRepository(session_factory)

    await repo.add(_memory(character_id="char-1", kind=MemoryKind.SEMANTIC, content="fact-a", salience=0.9))
    await repo.add(_memory(character_id="char-1", kind=MemoryKind.EPISODIC, content="event-a", salience=0.9))
    await repo.add(_memory(character_id="char-1", kind=MemoryKind.SEMANTIC, content="fact-b-trivial", salience=0.1))

    semantic_only = await repo.query("char-1", kinds=[MemoryKind.SEMANTIC], limit=10)
    assert {item.content for item in semantic_only} == {"fact-a", "fact-b-trivial"}

    salient_only = await repo.query("char-1", limit=10, min_salience=0.5)
    assert {item.content for item in salient_only} == {"fact-a", "event-a"}


@pytest.mark.asyncio
async def test_memory_add_many_and_roundtrip(session_factory: sessionmaker) -> None:
    repo = SAMemoryRepository(session_factory)
    items = [
        _memory(character_id="char-1", kind=MemoryKind.SEMANTIC, content="fact-1", tags=("a", "b")),
        _memory(character_id="char-1", kind=MemoryKind.RELATIONSHIP, content="rel-1", salience=0.8),
    ]
    await repo.add_many(items)

    result = await repo.query("char-1", limit=10)
    contents = {item.content for item in result}
    assert contents == {"fact-1", "rel-1"}
    fact = next(i for i in result if i.content == "fact-1")
    assert fact.tags == ("a", "b")


@pytest.mark.asyncio
async def test_memory_touch_updates_access_stats(session_factory: sessionmaker) -> None:
    repo = SAMemoryRepository(session_factory)
    item = _memory(character_id="char-1", kind=MemoryKind.SEMANTIC, content="x")
    await repo.add(item)

    await repo.touch(item.id)
    [stored] = await repo.query("char-1", limit=10)
    assert stored.access_count == 1
    assert stored.last_accessed_at is not None


@pytest.mark.asyncio
async def test_memory_empty_character(session_factory: sessionmaker) -> None:
    repo = SAMemoryRepository(session_factory)
    result = await repo.query("nonexistent", limit=5)
    assert result == []


@pytest.mark.asyncio
async def test_memory_query_returns_timezone_aware_datetimes(session_factory: sessionmaker) -> None:
    """SQLite strips tzinfo on read; the mapper must reattach UTC."""
    repo = SAMemoryRepository(session_factory)
    await repo.add(_memory(character_id="char-1", kind=MemoryKind.SEMANTIC, content="fact"))
    [item] = await repo.query("char-1", limit=10)
    assert item.created_at.tzinfo is not None
    assert item.created_at.utcoffset() == timedelta(0)


# ---------- Delete cascade ----------


@pytest.mark.asyncio
async def test_character_delete_removes_row(session_factory: sessionmaker) -> None:
    repo = SACharacterRepository(session_factory)
    character = Character.create(
        name="Temp", summary="", personality=[], interests=[],
        speaking_style="", boundaries=[],
        state=CharacterState(emotion="neutral", affection=50, fatigue=0, trust=50, energy=100),
    )
    await repo.save(character)

    assert await repo.delete(character.id) is True
    assert await repo.get(character.id) is None
    assert await repo.delete(character.id) is False  # idempotent


@pytest.mark.asyncio
async def test_conversation_delete_for_character_cascades_messages(
    session_factory: sessionmaker,
) -> None:
    repo = SAConversationRepository(session_factory)

    conv = Conversation.start(character_id="char-A")
    conv = conv.append(Message(role=MessageRole.USER, content="hi"))
    conv = conv.append(Message(role=MessageRole.ASSISTANT, content="hello"))
    await repo.save(conv)

    keeper = Conversation.start(character_id="char-B")
    keeper = keeper.append(Message(role=MessageRole.USER, content="other"))
    await repo.save(keeper)

    removed = await repo.delete_for_character("char-A")
    assert removed == 1
    assert await repo.get(conv.id) is None
    assert await repo.get(keeper.id) is not None


@pytest.mark.asyncio
async def test_memory_delete_for_character_removes_only_target(
    session_factory: sessionmaker,
) -> None:
    repo = SAMemoryRepository(session_factory)
    await repo.add(_memory(character_id="char-A", kind=MemoryKind.SEMANTIC, content="a"))
    await repo.add(_memory(character_id="char-A", kind=MemoryKind.EPISODIC, content="b"))
    await repo.add(_memory(character_id="char-B", kind=MemoryKind.SEMANTIC, content="c"))

    removed = await repo.delete_for_character("char-A")
    assert removed == 2
    assert await repo.query("char-A", limit=10) == []
    remaining = await repo.query("char-B", limit=10)
    assert len(remaining) == 1


# ---------- Schedule ----------


def _schedule(
    character_id: str,
    civil_date,  # date
    activities: list[tuple[int, int, str, str, str | None]] | None = None,
):
    from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity

    built: list[ScheduleActivity] = []
    for start_h, end_h, description, category, location in activities or []:
        built.append(
            ScheduleActivity.create(
                start_at=datetime(
                    civil_date.year, civil_date.month, civil_date.day,
                    start_h, 0, tzinfo=timezone.utc,
                ),
                end_at=datetime(
                    civil_date.year, civil_date.month, civil_date.day,
                    end_h, 0, tzinfo=timezone.utc,
                ),
                description=description,
                category=category,
                location=location,
            )
        )
    return DailySchedule.create(
        character_id=character_id,
        date_=civil_date,
        activities=built,
    )


@pytest.mark.asyncio
async def test_schedule_save_and_get(session_factory: sessionmaker) -> None:
    from datetime import date as date_cls

    repo = SAScheduleRepository(session_factory)
    target = date_cls(2026, 4, 18)
    schedule = _schedule(
        "char-A", target,
        [
            (9, 12, "上午工作", "work", "辦公室"),
            (14, 18, "會議", "meeting", None),
        ],
    )
    await repo.save(schedule)

    loaded = await repo.get("char-A", target)
    assert loaded is not None
    assert len(loaded.activities) == 2
    assert loaded.activities[0].description == "上午工作"
    assert loaded.activities[0].location == "辦公室"
    assert loaded.activities[1].location is None


@pytest.mark.asyncio
async def test_schedule_activity_has_memory_round_trips(
    session_factory: sessionmaker,
) -> None:
    from datetime import date as date_cls
    from kokoro_link.domain.entities.schedule import DailySchedule, ScheduleActivity

    repo = SAScheduleRepository(session_factory)
    target = date_cls(2026, 4, 18)
    activity = ScheduleActivity.create(
        start_at=datetime(2026, 4, 18, 9, 0, tzinfo=timezone.utc),
        end_at=datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc),
        description="上午工作",
        category="work",
        memorialized=True,
        has_memory=True,
    )
    await repo.save(
        DailySchedule.create(character_id="char-A", date_=target, activities=[activity]),
    )

    loaded = await repo.get("char-A", target)

    assert loaded is not None
    assert loaded.activities[0].memorialized is True
    assert loaded.activities[0].has_memory is True


@pytest.mark.asyncio
async def test_schedule_upsert_by_character_and_date(session_factory: sessionmaker) -> None:
    from datetime import date as date_cls

    repo = SAScheduleRepository(session_factory)
    target = date_cls(2026, 4, 18)
    first = _schedule("char-A", target, [(9, 10, "first", "work", None)])
    await repo.save(first)

    # New UUID, same (character, date) → should replace
    second = _schedule("char-A", target, [(11, 12, "second", "work", None)])
    await repo.save(second)

    loaded = await repo.get("char-A", target)
    assert loaded is not None
    assert len(loaded.activities) == 1
    assert loaded.activities[0].description == "second"


@pytest.mark.asyncio
async def test_schedule_list_for_character_newest_first(
    session_factory: sessionmaker,
) -> None:
    from datetime import date as date_cls

    repo = SAScheduleRepository(session_factory)
    await repo.save(_schedule("char-A", date_cls(2026, 4, 17), [(9, 10, "a", "x", None)]))
    await repo.save(_schedule("char-A", date_cls(2026, 4, 18), [(9, 10, "b", "x", None)]))
    await repo.save(_schedule("char-A", date_cls(2026, 4, 19), [(9, 10, "c", "x", None)]))

    listing = await repo.list_for_character("char-A")
    assert [s.date.isoformat() for s in listing] == [
        "2026-04-19", "2026-04-18", "2026-04-17",
    ]


@pytest.mark.asyncio
async def test_schedule_delete_for_character_cascades_activities(
    session_factory: sessionmaker,
) -> None:
    from datetime import date as date_cls

    repo = SAScheduleRepository(session_factory)
    await repo.save(_schedule("char-A", date_cls(2026, 4, 18), [(9, 10, "x", "y", None)]))
    await repo.save(_schedule("char-A", date_cls(2026, 4, 19), [(9, 10, "x", "y", None)]))
    await repo.save(_schedule("char-B", date_cls(2026, 4, 18), [(9, 10, "x", "y", None)]))

    removed = await repo.delete_for_character("char-A")
    assert removed == 2
    assert await repo.list_for_character("char-A") == []
    remaining = await repo.list_for_character("char-B")
    assert len(remaining) == 1


@pytest.mark.asyncio
async def test_schedule_get_returns_timezone_aware_datetimes(
    session_factory: sessionmaker,
) -> None:
    from datetime import date as date_cls

    repo = SAScheduleRepository(session_factory)
    target = date_cls(2026, 4, 18)
    await repo.save(_schedule("char-A", target, [(9, 10, "x", "y", None)]))

    loaded = await repo.get("char-A", target)
    assert loaded is not None
    assert loaded.generated_at.tzinfo is not None
    assert loaded.activities[0].start_at.tzinfo is not None


# ---------- Operator profile: Cloud->Core tenant-tier bulk push ----------


def _cloud_operator(op_id: str, tenant: str, tier: str) -> OperatorProfile:
    return OperatorProfile(
        id=op_id,
        display_name=op_id,
        cloud_account_id=f"acct-{op_id}",
        cloud_tenant_id=tenant,
        cloud_tenant_tier=tier,
        auth_provider="cloud",
    )


@pytest.mark.asyncio
async def test_set_cloud_tenant_tier_only_updates_target_tenant_cloud_operators(
    session_factory: sessionmaker,
) -> None:
    repo = SAOperatorProfileRepository(session_factory)
    await repo.save(_cloud_operator("op-a1", "tenant-A", "demo"))
    await repo.save(_cloud_operator("op-a2", "tenant-A", "demo"))
    await repo.save(_cloud_operator("op-b1", "tenant-B", "demo"))
    # A local operator sharing tenant-A's key must be excluded by the
    # ``auth_provider == 'cloud'`` guard.
    await repo.save(
        OperatorProfile(
            id="op-local",
            display_name="Local",
            cloud_tenant_id="tenant-A",
            cloud_tenant_tier="demo",
            auth_provider="local",
        )
    )

    updated = await repo.set_cloud_tenant_tier_for_cloud_tenant("tenant-A", " Plus ")

    assert updated == 2
    assert (await repo.get("op-a1")).cloud_tenant_tier == "plus"  # normalised
    assert (await repo.get("op-a2")).cloud_tenant_tier == "plus"
    assert (await repo.get("op-b1")).cloud_tenant_tier == "demo"
    assert (await repo.get("op-local")).cloud_tenant_tier == "demo"


@pytest.mark.asyncio
async def test_set_cloud_tenant_tier_blank_inputs_are_noops(
    session_factory: sessionmaker,
) -> None:
    repo = SAOperatorProfileRepository(session_factory)
    await repo.save(_cloud_operator("op-a1", "tenant-A", "demo"))

    assert await repo.set_cloud_tenant_tier_for_cloud_tenant("  ", "plus") == 0
    assert await repo.set_cloud_tenant_tier_for_cloud_tenant("tenant-A", "  ") == 0
    assert (await repo.get("op-a1")).cloud_tenant_tier == "demo"


@pytest.mark.asyncio
async def test_ordinary_save_does_not_overwrite_pushed_tenant_tier(
    session_factory: sessionmaker,
) -> None:
    # H3 lost-update regression: tier is authoritative ONLY via the dedicated
    # push path (+ the first INSERT). An ordinary aggregate save carrying a
    # STALE tier (e.g. a login re-projection racing/after a push) must NOT
    # revert the pushed value.
    repo = SAOperatorProfileRepository(session_factory)
    await repo.save(_cloud_operator("op-a1", "tenant-A", "demo"))

    assert await repo.set_cloud_tenant_tier_for_cloud_tenant("tenant-A", "plus") == 1
    assert (await repo.get("op-a1")).cloud_tenant_tier == "plus"

    # Re-save the aggregate carrying the OLD "demo" tier but a NEW display name.
    stale = _cloud_operator("op-a1", "tenant-A", "demo").update(
        display_name="Renamed",
    )
    await repo.save(stale)

    reloaded = await repo.get("op-a1")
    assert reloaded.display_name == "Renamed"  # ordinary fields still persist
    assert reloaded.cloud_tenant_tier == "plus"  # pushed tier preserved
