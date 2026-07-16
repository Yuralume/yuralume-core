from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.routes.characters import router
from kokoro_link.contracts.scene_access import (
    StageAccessAction,
    StageAccessDecision,
    StageAccessVerdict,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.presence_frame import AccessContext, ChatSurface


class _StageAccessService:
    def __init__(self, verdict: StageAccessVerdict) -> None:
        self.verdict = verdict
        self.calls: list[tuple[str, str, ChatSurface, str]] = []

    async def evaluate(
        self,
        character_id: str,
        *,
        operator_id: str,
        requested_surface: ChatSurface,
        current_user_id: str | None = None,
    ) -> StageAccessVerdict:
        self.calls.append((
            character_id,
            operator_id,
            requested_surface,
            current_user_id or "",
        ))
        return self.verdict


class _CharacterService:
    def __init__(self, character: Character) -> None:
        self.character = character

    async def get_character_entity(
        self,
        character_id: str,
        user_id: str | None = None,
    ) -> Character | None:
        if character_id != self.character.id:
            return None
        if user_id is not None and user_id != self.character.user_id:
            return None
        return self.character


@dataclass
class _Container:
    scene_access_service: _StageAccessService
    character_service: _CharacterService
    app_settings = None
    operator_profile_repository = None


def _character() -> Character:
    return Character.create(
        name="Mio",
        summary="",
        personality=[],
        interests=[],
        speaking_style="",
        boundaries=[],
        state=CharacterState(
            emotion="平靜",
            affection=0,
            fatigue=0,
            trust=0,
            energy=80,
        ),
    )


def _client(container: _Container) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1")
    app.state.container = container
    return TestClient(app)


def test_stage_access_route_returns_block_verdict() -> None:
    character = _character()
    service = _StageAccessService(
        StageAccessVerdict(
            decision=StageAccessDecision.BLOCK,
            recommended_action=StageAccessAction.USE_PHONE,
            access_context=AccessContext.TEXT_MESSAGE_ONLY,
            reason_for_user="現在比較適合先傳訊息。",
            prompt_fact="使用者不應被放進角色當前場景。",
            suggested_opener="你現在方便聊一下嗎？",
        ),
    )
    client = _client(_Container(service, _CharacterService(character)))

    response = client.get(f"/api/v1/characters/{character.id}/stage-access")

    assert response.status_code == 200
    assert response.json() == {
        "decision": "block",
        "recommended_action": "use_phone",
        "access_context": "text_message_only",
        "reason_for_user": "現在比較適合先傳訊息。",
        "prompt_fact": "使用者不應被放進角色當前場景。",
        "suggested_opener": "你現在方便聊一下嗎？",
    }
    assert service.calls == [
        (character.id, DEFAULT_OPERATOR_ID, ChatSurface.WEB_STAGE, DEFAULT_OPERATOR_ID),
    ]


def test_stage_access_route_returns_allow_verdict() -> None:
    character = _character()
    service = _StageAccessService(
        StageAccessVerdict(
            decision=StageAccessDecision.ALLOW,
            recommended_action=StageAccessAction.USE_STAGE,
            access_context=AccessContext.PUBLIC_ENCOUNTER,
            reason_for_user="她在開放場景，適合自然碰面。",
            prompt_fact="可在公共場景中自然偶遇。",
        ),
    )
    client = _client(_Container(service, _CharacterService(character)))

    response = client.get(f"/api/v1/characters/{character.id}/stage-access")

    assert response.status_code == 200
    assert response.json()["decision"] == "allow"
    assert response.json()["access_context"] == "public_encounter"
