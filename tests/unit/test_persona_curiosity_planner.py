from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Sequence

import pytest

from kokoro_link.application.services.feature_keys import FEATURE_PERSONA_CURIOSITY
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.contracts.persona_curiosity import (
    PersonaCuriosityAttemptSummary,
    PersonaCuriosityContext,
)
from kokoro_link.infrastructure.persona.llm_curiosity_planner import (
    LLMPersonaCuriosityPlanner,
    NullPersonaCuriosityPlanner,
)


class _ScriptedModel(ChatModelPort):
    provider_id = "scripted"
    supports_vision = False

    def __init__(self, response: str | Exception) -> None:
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
        if isinstance(self.response, Exception):
            raise self.response
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
        self.resolve_calls: list[tuple[str | None, str | None, str | None]] = []
        self.model_id_calls: list[tuple[str | None, str | None, str | None]] = []
        self.fake_calls: list[tuple[str | None, str | None, str | None]] = []

    async def resolve(self, feature_key=None, *, character=None):
        self.resolve_calls.append((
            feature_key,
            getattr(character, "id", None),
            getattr(character, "user_id", None),
        ))
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None):
        self.model_id_calls.append((
            feature_key,
            getattr(character, "id", None),
            getattr(character, "user_id", None),
        ))
        return "scripted-model"

    async def is_fake(self, feature_key=None, *, character=None):
        self.fake_calls.append((
            feature_key,
            getattr(character, "id", None),
            getattr(character, "user_id", None),
        ))
        return self.fake


def _context() -> PersonaCuriosityContext:
    now = datetime(2026, 6, 9, 12, 0, tzinfo=timezone.utc)
    return PersonaCuriosityContext(
        character_id="char-A",
        operator_id="default",
        surface="chat",
        known_profile_summary=("稱呼偏好：小丹",),
        profile_gaps=(
            "還不清楚使用者的日常節奏與常見空閒時間。",
            "還不清楚使用者希望角色怎麼陪伴或回應。",
        ),
        sensitive_boundaries=(
            "Layer 1/2 可低壓探索；一次最多一個自然問題。",
            "Layer 3/5 屬於敏感資訊，除非使用者已主動打開話題，否則不要主動逼問。",
            "不要提到使用者畫像、資料蒐集、補欄位或問卷。",
        ),
        recent_curiosity_attempts=(
            PersonaCuriosityAttemptSummary(
                surface="chat",
                target_layer=2,
                target_topic="routine",
                question_intent="learn daily rhythm without survey wording",
                status="asked",
                created_at=now,
            ),
        ),
        recent_dialogue_summary="玩家剛說今天很累，但還願意繼續聊。",
        interaction_strength=(
            "還沒有足夠互動紀錄；探索語氣應以最近對話與起始關係設定校準，"
            "不可因此覆寫關係主述。"
        ),
        initial_relationship_summary=(
            "起始關係設定是關係主述，互動量低時不可覆寫這份關係。",
            "- 關係：老朋友",
        ),
        now=now,
    )


@pytest.mark.asyncio
async def test_llm_persona_curiosity_planner_parses_structured_plan() -> None:
    response = json.dumps(
        {
            "should_ask": True,
            "target_layer": "layer2",
            "target_topic": "companion_preference",
            "tone_strategy": "casual_self_disclosure",
            "question_intent": "learn how the user wants the character to respond",
            "safety_reason": "recent dialogue invites a low-pressure check-in",
            "avoid": [
                "do not ask multiple questions",
                "do not mention profile collection",
            ],
        },
        ensure_ascii=False,
    )
    model = _ScriptedModel(response)
    provider = _ActiveProvider(model)
    planner = LLMPersonaCuriosityPlanner(
        provider=provider,
        feature_key=FEATURE_PERSONA_CURIOSITY,
    )

    plan = await planner.plan(_context())

    assert plan.should_ask is True
    assert plan.target_layer == 2
    assert plan.target_topic == "companion_preference"
    assert plan.tone_strategy == "casual_self_disclosure"
    assert plan.question_intent.startswith("learn how")
    assert "low-pressure" in plan.safety_reason
    assert "do not ask multiple questions" in plan.avoid
    assert plan.planner_metadata["provider_id"] == "scripted"
    assert plan.planner_metadata["model_id"] == "scripted-model"
    assert plan.planner_metadata["latency_ms"] >= 0
    assert plan.planner_metadata["recent_attempt_count"] == 1
    assert provider.fake_calls == [(FEATURE_PERSONA_CURIOSITY, "char-A", "default")]
    assert provider.resolve_calls == [(FEATURE_PERSONA_CURIOSITY, "char-A", "default")]
    assert provider.model_id_calls == [(FEATURE_PERSONA_CURIOSITY, "char-A", "default")]
    assert model.calls[0][1] == "scripted-model"
    prompt = model.calls[0][0]
    assert "稱呼偏好：小丹" in prompt
    assert "起始關係設定是關係主述" in prompt
    assert "關係：老朋友" in prompt
    assert "最近探索紀錄" in prompt
    assert "不要提到使用者畫像" in prompt
    assert "一次最多一個" in prompt


@pytest.mark.asyncio
async def test_llm_persona_curiosity_planner_injects_operator_language_hint() -> None:
    # Regression: question_intent renders in the Observability "current intent"
    # panel, so an en-US operator must not see a Chinese intent line.
    model = _ScriptedModel(json.dumps({"should_ask": False, "safety_reason": "no"}))
    planner = LLMPersonaCuriosityPlanner(model=model)

    await planner.plan(replace(_context(), operator_primary_language="en-US"))

    prompt = model.calls[0][0]
    assert "en-US" in prompt
    assert "玩家可見自然語言輸出語言" in prompt


@pytest.mark.asyncio
async def test_llm_persona_curiosity_planner_fail_soft_on_bad_json() -> None:
    planner = LLMPersonaCuriosityPlanner(model=_ScriptedModel("not json"))

    plan = await planner.plan(_context())

    assert plan.should_ask is False
    assert plan.safety_reason == "planner unavailable"
    assert plan.planner_metadata["provider_id"] == "scripted"
    assert plan.planner_metadata["model_id"] == "scripted"
    assert plan.planner_metadata["recent_attempt_count"] == 1


@pytest.mark.asyncio
async def test_llm_persona_curiosity_planner_treats_string_false_as_no_ask() -> None:
    response = json.dumps(
        {
            "should_ask": "false",
            "target_layer": 2,
            "target_topic": "routine",
            "tone_strategy": "casual",
            "question_intent": "learn daily rhythm",
            "safety_reason": "low-pressure topic",
        },
    )
    planner = LLMPersonaCuriosityPlanner(model=_ScriptedModel(response))

    plan = await planner.plan(_context())

    assert plan.should_ask is False
    assert plan.target_topic == ""


@pytest.mark.asyncio
async def test_llm_persona_curiosity_planner_uses_real_character_for_routing() -> None:
    response = json.dumps(
        {
            "should_ask": False,
            "safety_reason": "not a good moment",
        },
    )
    model = _ScriptedModel(response)
    provider = _ActiveProvider(model)
    planner = LLMPersonaCuriosityPlanner(
        provider=provider,
        feature_key=FEATURE_PERSONA_CURIOSITY,
    )
    character = SimpleNamespace(
        id="char-A",
        user_id="user-42",
        feature_models=(),
        feature_model_for=lambda feature_key: None,
    )

    await planner.plan(_context(), character=character)

    assert provider.fake_calls == [(FEATURE_PERSONA_CURIOSITY, "char-A", "user-42")]
    assert provider.resolve_calls == [(FEATURE_PERSONA_CURIOSITY, "char-A", "user-42")]


@pytest.mark.asyncio
async def test_llm_persona_curiosity_planner_fake_provider_returns_no_ask() -> None:
    model = _ScriptedModel('{"should_ask": true}')
    planner = LLMPersonaCuriosityPlanner(
        provider=_ActiveProvider(model, fake=True),
        feature_key=FEATURE_PERSONA_CURIOSITY,
    )

    plan = await planner.plan(_context())

    assert plan.should_ask is False
    assert model.calls == []


@pytest.mark.asyncio
async def test_null_persona_curiosity_planner_is_noop() -> None:
    plan = await NullPersonaCuriosityPlanner().plan(_context())

    assert plan.should_ask is False
    assert plan.target_topic == ""
