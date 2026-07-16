from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container
from kokoro_link.api.routes.characters import router
from kokoro_link.application.services.character_creation_intake_service import (
    CharacterCreationIntakeService,
)


class _FakeModel:
    supports_vision = False

    async def generate(self, prompt: str, *, image_urls=None):  # noqa: ARG002
        return (
            '{"can_create": false, "missing_required": ["known_context"], '
            '"questions": [{"field": "known_context", "question": "她可以知道你們怎麼認識的嗎？"}], '
            '"normalized_relationship": {}, "normalized_user_profile": {}, "warnings": []}'
        )

    def generate_stream(self, prompt: str, *, image_urls=None):  # noqa: ARG002
        async def _empty():
            if False:
                yield ""
        return _empty()


class _CapturingIntakeService(CharacterCreationIntakeService):
    """Records the ``round_index`` the route hands the service."""

    def __init__(self, model: _FakeModel) -> None:
        super().__init__(model=model)
        self.seen_round_index: int | None = None

    async def analyze(self, **kwargs):  # noqa: ANN003
        self.seen_round_index = kwargs.get("round_index")
        return await super().analyze(**kwargs)


@dataclass
class _Container:
    character_creation_intake_service: CharacterCreationIntakeService | None


def _client(container: _Container) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def test_analyze_endpoint_round_trips_questions() -> None:
    client = _client(_Container(
        character_creation_intake_service=CharacterCreationIntakeService(
            model=_FakeModel(),
        ),
    ))

    resp = client.post(
        "/api/v1/characters/creation-intake/analyze",
        json={
            "character_draft": {"name": "澪"},
            "relationship": {"relationship_label": "朋友"},
            "current_locale": "zh-TW",
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["can_create"] is False
    assert body["questions"][0]["field"] == "known_context"


def test_analyze_endpoint_clamps_out_of_range_round_index() -> None:
    # An over-counting client used to send round_index > 2 and get a 422,
    # which wiped the already-shown suggestions. The handler now clamps.
    service = _CapturingIntakeService(model=_FakeModel())
    client = _client(_Container(character_creation_intake_service=service))

    resp = client.post(
        "/api/v1/characters/creation-intake/analyze",
        json={
            "character_draft": {"name": "澪"},
            "relationship": {"relationship_label": "朋友"},
            "round_index": 5,
        },
    )

    assert resp.status_code == 200
    assert service.seen_round_index == 2


def test_analyze_endpoint_still_rejects_negative_round_index() -> None:
    client = _client(_Container(
        character_creation_intake_service=CharacterCreationIntakeService(
            model=_FakeModel(),
        ),
    ))

    resp = client.post(
        "/api/v1/characters/creation-intake/analyze",
        json={"character_draft": {"name": "澪"}, "round_index": -1},
    )

    assert resp.status_code == 422


def test_analyze_endpoint_returns_503_when_service_missing() -> None:
    client = _client(_Container(character_creation_intake_service=None))
    resp = client.post("/api/v1/characters/creation-intake/analyze", json={})
    assert resp.status_code == 503


def test_create_character_rejects_unknown_personality_type_at_api_boundary() -> None:
    client = _client(_Container(character_creation_intake_service=None))
    resp = client.post(
        "/api/v1/characters",
        json={
            "name": "澪",
            "personality_type": {"code": "XXXX"},
        },
    )
    assert resp.status_code == 422


def test_create_character_rejects_unknown_schedule_policy_at_api_boundary() -> None:
    client = _client(_Container(character_creation_intake_service=None))
    resp = client.post(
        "/api/v1/characters",
        json={
            "name": "澪",
            "initial_relationship": {
                "schedule_involvement_policy": "move_in_together",
            },
        },
    )
    assert resp.status_code == 422
