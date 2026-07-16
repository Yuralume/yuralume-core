from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime, timezone
from typing import Sequence

import pytest

from kokoro_link.application.services.feature_keys import (
    FEATURE_PERSONA_PROJECTION,
)
from kokoro_link.application.services.operator_persona_projection_service import (
    OperatorPersonaProjectionCharacterNotFoundError,
    OperatorPersonaProjectionService,
)
from kokoro_link.contracts.llm import ChatModelPort
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.profile_field import (
    EvidenceRef,
    ProfileField,
)


_CHAR_ID = "char-A"
_OTHER_USER = "someone-else"


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
        self.model_id_calls: list[str | None] = []
        self.fake_calls: list[str | None] = []

    async def resolve(self, feature_key=None, *, character=None):
        self.resolve_calls.append(feature_key)
        return self.model

    async def resolve_model_id(self, feature_key=None, *, character=None):
        self.model_id_calls.append(feature_key)
        return "scripted-model"

    async def is_fake(self, feature_key=None, *, character=None):
        self.fake_calls.append(feature_key)
        return self.fake


class _CharacterService:
    def __init__(self, character: Character | None) -> None:
        self.character = character
        self.calls: list[tuple[str, str | None]] = []

    async def get_character_entity(
        self,
        character_id: str,
        *,
        user_id: str | None = None,
    ) -> Character | None:
        self.calls.append((character_id, user_id))
        if self.character is None:
            return None
        if self.character.id != character_id:
            return None
        if user_id is not None and self.character.user_id != user_id:
            return None
        return self.character


class _PersonaService:
    def __init__(self, persona: OperatorPersona) -> None:
        self.persona = persona
        self.calls: list[tuple[str, str]] = []

    async def get_current(self, character_id: str, operator_id: str) -> OperatorPersona:
        self.calls.append((character_id, operator_id))
        return self.persona


def _character(*, user_id: str = DEFAULT_OPERATOR_ID) -> Character:
    character = Character.create(
        name="小雨",
        summary="在城市角落寫故事的人。",
        personality=["敏銳"],
        interests=["咖啡店"],
        speaking_style="溫柔",
        boundaries=[],
        state=CharacterState(
            emotion="calm",
            affection=50,
            fatigue=10,
            trust=50,
            energy=80,
        ),
        user_id=user_id,
    )
    return replace(character, id=_CHAR_ID)


def _field(
    field_key: str,
    layer: int,
    value: str,
    *,
    confidence: float = 0.85,
    field_id: str | None = None,
    content_mode: MessageContentMode = MessageContentMode.NORMAL,
) -> ProfileField:
    return ProfileField(
        field_key=field_key,
        layer=layer,
        value=value,
        confidence=confidence,
        evidence_refs=(
            EvidenceRef(
                turn_id="turn-1",
                conversation_id="conv-1",
                quote=value,
                extracted_at=datetime(2026, 5, 17, tzinfo=timezone.utc),
            ),
        ),
        last_updated=datetime(2026, 5, 17, tzinfo=timezone.utc),
        update_count=2,
        source="extraction",
        character_id=_CHAR_ID,
        field_id=field_id or f"fld-{field_key}",
        content_mode=content_mode,
    )


@pytest.mark.asyncio
async def test_projection_generates_narrative_from_safe_layer1_and_2_only() -> None:
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=DEFAULT_OPERATOR_ID,
        layer1_identity={
            "name": _field("name", 1, "丹尼"),
            "occupation": _field("occupation", 1, "後端工程師"),
            "family": _field("family", 1, "有一個妹妹"),
        },
        layer2_life={
            "interests": _field("interests", 2, "爵士樂和獨立遊戲"),
            "relationship_status": _field("relationship_status", 2, "單身"),
            "diet": _field("diet", 2, "不吃辣", confidence=0.65),
        },
        layer3_emotional={
            "anxieties": _field("anxieties", 3, "擔心工作失敗", confidence=0.95),
        },
        layer5_trust={
            "secret_kept": _field("secret_kept", 5, "託付過秘密", confidence=0.95),
        },
    )
    model = _ScriptedModel('{"narrative":"我記得你常把事情整理得很仔細，也會被爵士樂吸引。"}')
    provider = _ActiveProvider(model)
    service = OperatorPersonaProjectionService(
        character_service=_CharacterService(_character()),
        persona_service=_PersonaService(persona),
        active_llm_provider=provider,  # type: ignore[arg-type]
    )

    response = await service.project(_CHAR_ID, operator_id=DEFAULT_OPERATOR_ID)

    assert response.narrative == "我記得你常把事情整理得很仔細，也會被爵士樂吸引。"
    assert provider.fake_calls == [FEATURE_PERSONA_PROJECTION]
    assert provider.resolve_calls == [FEATURE_PERSONA_PROJECTION]
    prompt, model_id = model.calls[0]
    assert model_id == "scripted-model"
    assert "後端工程師" in prompt
    assert "爵士樂和獨立遊戲" in prompt
    assert "妹妹" not in prompt
    assert "單身" not in prompt
    assert "不吃辣" not in prompt
    assert "工作失敗" not in prompt
    assert "託付過秘密" not in prompt
    assert [fact.field_id for fact in response.facts] == [
        "fld-name",
        "fld-occupation",
        "fld-interests",
    ]


@pytest.mark.asyncio
async def test_projection_excludes_nsfw_mode_layer1_and_2_facts() -> None:
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=DEFAULT_OPERATOR_ID,
        layer1_identity={
            "occupation": _field(
                "occupation", 1, "後端工程師",
                content_mode=MessageContentMode.NSFW,
            ),
        },
        layer2_life={
            "interests": _field(
                "interests", 2, "爵士樂",
                content_mode=MessageContentMode.NSFW,
            ),
        },
    )
    model = _ScriptedModel('{"narrative":"should not call"}')
    service = OperatorPersonaProjectionService(
        character_service=_CharacterService(_character()),
        persona_service=_PersonaService(persona),
        active_llm_provider=_ActiveProvider(model),  # type: ignore[arg-type]
    )

    response = await service.project(_CHAR_ID, operator_id=DEFAULT_OPERATOR_ID)

    assert response.empty is True
    assert response.facts == []
    assert model.calls == []


@pytest.mark.asyncio
async def test_projection_returns_facts_without_calling_fake_model() -> None:
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=DEFAULT_OPERATOR_ID,
        layer1_identity={"occupation": _field("occupation", 1, "後端工程師")},
    )
    model = _ScriptedModel('{"narrative":"should not call"}')
    service = OperatorPersonaProjectionService(
        character_service=_CharacterService(_character()),
        persona_service=_PersonaService(persona),
        active_llm_provider=_ActiveProvider(model, fake=True),  # type: ignore[arg-type]
    )

    response = await service.project(_CHAR_ID, operator_id=DEFAULT_OPERATOR_ID)

    assert response.narrative == ""
    assert response.empty is False
    assert [fact.value for fact in response.facts] == ["後端工程師"]
    assert model.calls == []


@pytest.mark.asyncio
async def test_projection_caches_llm_result_until_invalidated() -> None:
    persona = OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=DEFAULT_OPERATOR_ID,
        layer1_identity={"occupation": _field("occupation", 1, "後端工程師")},
    )
    model = _ScriptedModel('{"narrative":"第一次看見的你。"}')
    service = OperatorPersonaProjectionService(
        character_service=_CharacterService(_character()),
        persona_service=_PersonaService(persona),
        active_llm_provider=_ActiveProvider(model),  # type: ignore[arg-type]
    )

    first = await service.project(_CHAR_ID, operator_id=DEFAULT_OPERATOR_ID)
    model.response = '{"narrative":"重新整理後的你。"}'
    cached = await service.project(_CHAR_ID, operator_id=DEFAULT_OPERATOR_ID)
    service.invalidate(_CHAR_ID, DEFAULT_OPERATOR_ID)
    refreshed = await service.project(_CHAR_ID, operator_id=DEFAULT_OPERATOR_ID)

    assert first.narrative == "第一次看見的你。"
    assert cached.narrative == "第一次看見的你。"
    assert refreshed.narrative == "重新整理後的你。"
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_projection_collapses_cross_user_character_to_not_found() -> None:
    persona = OperatorPersona.empty(_CHAR_ID, DEFAULT_OPERATOR_ID)
    service = OperatorPersonaProjectionService(
        character_service=_CharacterService(_character(user_id=_OTHER_USER)),
        persona_service=_PersonaService(persona),
        active_llm_provider=_ActiveProvider(_ScriptedModel("{}")),  # type: ignore[arg-type]
    )

    with pytest.raises(OperatorPersonaProjectionCharacterNotFoundError):
        await service.project(_CHAR_ID, operator_id=DEFAULT_OPERATOR_ID)
