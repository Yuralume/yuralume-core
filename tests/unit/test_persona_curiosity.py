from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.persona_curiosity_service import (
    PersonaCuriosityService,
)
from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlan
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.persona_curiosity import (
    PERSONA_CURIOSITY_STATUS_ANSWERED,
    PERSONA_CURIOSITY_STATUS_ASKED,
    PERSONA_CURIOSITY_STATUS_PLANNED,
    PERSONA_CURIOSITY_SURFACE_CHAT,
    PERSONA_CURIOSITY_SURFACE_PROACTIVE,
    PersonaCuriosityAttempt,
)
from kokoro_link.domain.value_objects.profile_field import (
    EvidenceRef,
    ProfileField,
)
from kokoro_link.infrastructure.repositories.in_memory_persona_curiosity import (
    InMemoryPersonaCuriosityRepository,
)


_CHAR = "char-A"
_OP = "default"
_NOW = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)


def _evidence(quote: str = "我平常會畫圖") -> EvidenceRef:
    return EvidenceRef(
        turn_id="turn-1",
        conversation_id="conv-1",
        quote=quote,
        extracted_at=_NOW,
    )


def _field(
    key: str,
    value: str,
    *,
    layer: int,
    confidence: float = 0.9,
    content_mode: MessageContentMode = MessageContentMode.NORMAL,
) -> ProfileField:
    return ProfileField(
        field_key=key,
        layer=layer,
        value=value,
        confidence=confidence,
        evidence_refs=(_evidence(),),
        last_updated=_NOW,
        update_count=1,
        source="extraction",
        content_mode=content_mode,
        character_id=_CHAR,
    )


def test_attempt_requires_pair_scope() -> None:
    with pytest.raises(ValueError, match="character_id"):
        PersonaCuriosityAttempt.new(
            character_id="",
            operator_id=_OP,
            surface=PERSONA_CURIOSITY_SURFACE_CHAT,
            target_layer=2,
            target_topic="routine",
            question_intent="learn daily rhythm",
            created_at=_NOW,
        )


def test_attempt_normalizes_metadata_and_status() -> None:
    attempt = PersonaCuriosityAttempt.new(
        character_id=_CHAR,
        operator_id=_OP,
        surface=PERSONA_CURIOSITY_SURFACE_CHAT,
        target_layer=2,
        target_topic="routine",
        question_intent="learn daily rhythm",
        created_at=_NOW,
        metadata={"empty": "", "count": 2},
    )

    assert attempt.status == PERSONA_CURIOSITY_STATUS_PLANNED
    assert attempt.surface == PERSONA_CURIOSITY_SURFACE_CHAT
    assert attempt.metadata == {"empty": "", "count": 2}


@pytest.mark.asyncio
async def test_in_memory_repo_lists_recent_per_pair_newest_first() -> None:
    repo = InMemoryPersonaCuriosityRepository()
    older = PersonaCuriosityAttempt.new(
        character_id=_CHAR,
        operator_id=_OP,
        surface=PERSONA_CURIOSITY_SURFACE_CHAT,
        target_layer=2,
        target_topic="routine",
        question_intent="learn daily rhythm",
        created_at=_NOW - timedelta(minutes=5),
    )
    newer = PersonaCuriosityAttempt.new(
        character_id=_CHAR,
        operator_id=_OP,
        surface=PERSONA_CURIOSITY_SURFACE_PROACTIVE,
        target_layer=1,
        target_topic="nickname",
        question_intent="learn preferred nickname",
        created_at=_NOW,
    )
    other = PersonaCuriosityAttempt.new(
        character_id="char-B",
        operator_id=_OP,
        surface=PERSONA_CURIOSITY_SURFACE_CHAT,
        target_layer=2,
        target_topic="interests",
        question_intent="learn interests",
        created_at=_NOW + timedelta(minutes=1),
    )

    await repo.add(older)
    await repo.add(newer)
    await repo.add(other)

    listed = await repo.list_recent(_CHAR, _OP, limit=10)
    assert [item.id for item in listed] == [newer.id, older.id]


@pytest.mark.asyncio
async def test_repo_updates_status_without_cross_pair_mutation() -> None:
    repo = InMemoryPersonaCuriosityRepository()
    attempt = PersonaCuriosityAttempt.new(
        character_id=_CHAR,
        operator_id=_OP,
        surface=PERSONA_CURIOSITY_SURFACE_CHAT,
        target_layer=2,
        target_topic="routine",
        question_intent="learn daily rhythm",
        created_at=_NOW,
    )
    await repo.add(attempt)

    assert await repo.mark_status(
        attempt.id,
        PERSONA_CURIOSITY_STATUS_ANSWERED,
        response_turn_id="turn-2",
    )
    updated = await repo.list_recent(_CHAR, _OP, limit=1)
    assert updated[0].status == PERSONA_CURIOSITY_STATUS_ANSWERED
    assert updated[0].response_turn_id == "turn-2"

    assert not await repo.mark_status(
        "missing-id",
        PERSONA_CURIOSITY_STATUS_ANSWERED,
    )


@pytest.mark.asyncio
async def test_context_describes_empty_persona_as_low_pressure_gaps() -> None:
    service = PersonaCuriosityService(repository=InMemoryPersonaCuriosityRepository())
    context = await service.build_context(
        persona=OperatorPersona.empty(_CHAR, _OP),
        surface=PERSONA_CURIOSITY_SURFACE_CHAT,
        recent_dialogue_summary="玩家剛開始聊天，還沒有透露太多。",
        now=_NOW,
    )

    assert context.known_profile_summary == ("目前還沒有穩定確認的使用者畫像。",)
    assert "稱呼或暱稱" in context.profile_gaps[0]
    assert any("Layer 3/5" in line for line in context.sensitive_boundaries)
    assert context.recent_curiosity_attempts == ()


@pytest.mark.asyncio
async def test_context_uses_confirmed_low_risk_facts_and_omits_known_gaps() -> None:
    persona = OperatorPersona(
        character_id=_CHAR,
        operator_id=_OP,
        layer1_identity={
            "nickname": _field("nickname", "小丹", layer=1),
        },
        layer2_life={
            "interests": _field("interests", "爵士樂和手沖咖啡", layer=2),
        },
        layer3_emotional={
            "anxieties": _field("anxieties", "害怕讓人失望", layer=3),
        },
    )
    service = PersonaCuriosityService(repository=InMemoryPersonaCuriosityRepository())
    context = await service.build_context(
        persona=persona,
        surface=PERSONA_CURIOSITY_SURFACE_CHAT,
        recent_dialogue_summary="玩家聊到今天有點累。",
        now=_NOW,
    )

    assert "稱呼偏好：小丹" in context.known_profile_summary
    assert "興趣：爵士樂和手沖咖啡" in context.known_profile_summary
    assert all("興趣" not in gap for gap in context.profile_gaps)
    assert all("害怕讓人失望" not in line for line in context.known_profile_summary)
    assert any("敏感資訊" in line for line in context.sensitive_boundaries)


@pytest.mark.asyncio
async def test_context_excludes_nsfw_mode_low_risk_facts() -> None:
    persona = OperatorPersona(
        character_id=_CHAR,
        operator_id=_OP,
        layer1_identity={
            "nickname": _field(
                "nickname",
                "NSFW 暱稱",
                layer=1,
                content_mode=MessageContentMode.NSFW,
            ),
        },
        layer2_life={
            "interests": _field(
                "interests",
                "NSFW 興趣",
                layer=2,
                content_mode=MessageContentMode.NSFW,
            ),
        },
    )
    service = PersonaCuriosityService(repository=InMemoryPersonaCuriosityRepository())

    context = await service.build_context(
        persona=persona,
        surface=PERSONA_CURIOSITY_SURFACE_CHAT,
        now=_NOW,
    )

    assert context.known_profile_summary == ("目前還沒有穩定確認的使用者畫像。",)
    assert any("稱呼或暱稱" in gap for gap in context.profile_gaps)
    assert any("興趣" in gap for gap in context.profile_gaps)


@pytest.mark.asyncio
async def test_context_surfaces_recent_attempts_as_facts_for_llm() -> None:
    repo = InMemoryPersonaCuriosityRepository()
    await repo.add(
        PersonaCuriosityAttempt.new(
            character_id=_CHAR,
            operator_id=_OP,
            surface=PERSONA_CURIOSITY_SURFACE_CHAT,
            target_layer=2,
            target_topic="routine",
            question_intent="learn daily rhythm without survey wording",
            status=PERSONA_CURIOSITY_STATUS_ASKED,
            created_at=_NOW - timedelta(minutes=10),
        ),
    )
    service = PersonaCuriosityService(repository=repo)

    context = await service.build_context(
        persona=OperatorPersona.empty(_CHAR, _OP),
        surface=PERSONA_CURIOSITY_SURFACE_PROACTIVE,
        recent_dialogue_summary="玩家昨天沒有回覆。",
        now=_NOW,
    )

    assert len(context.recent_curiosity_attempts) == 1
    assert context.recent_curiosity_attempts[0].target_topic == "routine"
    assert context.recent_curiosity_attempts[0].status == PERSONA_CURIOSITY_STATUS_ASKED


@pytest.mark.asyncio
async def test_service_records_should_ask_plan_as_planned_attempt() -> None:
    repo = InMemoryPersonaCuriosityRepository()
    service = PersonaCuriosityService(repository=repo)
    context = await service.build_context(
        persona=OperatorPersona.empty(_CHAR, _OP),
        surface=PERSONA_CURIOSITY_SURFACE_CHAT,
        now=_NOW,
    )
    plan = PersonaCuriosityPlan(
        should_ask=True,
        target_layer=2,
        target_topic="routine",
        tone_strategy="casual",
        question_intent="learn daily rhythm without survey wording",
        safety_reason="low-pressure Layer 2 topic",
        avoid=("do not mention profile collection",),
        planner_metadata={
            "provider_id": "openai",
            "latency_ms": 123,
            "non_json": object(),
        },
    )

    attempt = await service.record_planned_attempt(
        context=context,
        plan=plan,
        conversation_id="conv-1",
        now=_NOW,
    )

    assert attempt is not None
    assert attempt.status == PERSONA_CURIOSITY_STATUS_PLANNED
    assert attempt.conversation_id == "conv-1"
    assert attempt.target_topic == "routine"
    assert attempt.metadata["planner_metadata"]["provider_id"] == "openai"
    assert attempt.metadata["planner_metadata"]["latency_ms"] == 123
    assert isinstance(attempt.metadata["planner_metadata"]["non_json"], str)
    listed = await repo.list_recent(_CHAR, _OP, limit=5)
    assert [item.id for item in listed] == [attempt.id]


@pytest.mark.asyncio
async def test_service_does_not_record_no_ask_plan() -> None:
    repo = InMemoryPersonaCuriosityRepository()
    service = PersonaCuriosityService(repository=repo)
    context = await service.build_context(
        persona=OperatorPersona.empty(_CHAR, _OP),
        surface=PERSONA_CURIOSITY_SURFACE_CHAT,
        now=_NOW,
    )

    attempt = await service.record_planned_attempt(
        context=context,
        plan=PersonaCuriosityPlan.no_ask("recently asked"),
        now=_NOW,
    )

    assert attempt is None
    assert await repo.list_recent(_CHAR, _OP, limit=5) == []
