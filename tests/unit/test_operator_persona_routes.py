"""REST smoke tests for the operator-persona admin endpoints.

The routes are mostly thin pass-throughs to the service, so we
exercise the wiring (service unavailable → 503, snapshot → 200 with
the expected shape, state mutations → 204, bad state → 400) rather
than re-testing the business logic that lives in unit tests.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from kokoro_link.api.dependencies import get_container
from kokoro_link.api.routes.operator_persona import router
from kokoro_link.application.dto.operator_persona_projection import (
    PersonaProjectionFactResponse,
    PersonaProjectionResponse,
)
from kokoro_link.contracts.persona_consolidator import (
    ConsolidationResult,
    PromoteAction,
)
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.operator_persona import (
    InteractionStrength,
    OperatorPersona,
)
from kokoro_link.domain.value_objects.familiarity import Familiarity
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    EvidenceRef,
    ProfileField,
)


@dataclass
class _Container:
    operator_persona_service: object | None = None
    operator_persona_projection_service: object | None = None
    persona_dream_service: object | None = None
    relationship_seed_repository: object | None = None


def _client(container: _Container) -> TestClient:
    app = FastAPI()
    app.dependency_overrides[get_container] = lambda: container
    app.include_router(router, prefix="/api/v1")
    return TestClient(app)


def _evidence(quote: str = "我是工程師") -> EvidenceRef:
    return EvidenceRef(
        turn_id="t",
        conversation_id="c",
        quote=quote,
        extracted_at=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
    )


_CHAR_ID = "char-A"
_OP_ID = "default"


def _field(field_key: str, layer: int, value: str, conf: float = 0.85) -> ProfileField:
    return ProfileField(
        field_key=field_key,
        layer=layer,
        value=value,
        confidence=conf,
        evidence_refs=(_evidence(),),
        last_updated=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
        update_count=2,
        source="extraction",
        character_id=_CHAR_ID,
        field_id=f"fld-{field_key}",
    )


def _build_persona() -> OperatorPersona:
    return OperatorPersona(
        character_id=_CHAR_ID,
        operator_id=_OP_ID,
        layer1_identity={
            "name": _field("name", 1, "丹尼", conf=0.9),
            "occupation": _field("occupation", 1, "工程師", conf=0.8),
        },
        layer2_life={
            "interests": _field("interests", 2, "科幻電影", conf=0.75),
        },
        layer4_interaction=InteractionStrength(
            character_id=_CHAR_ID,
            operator_id=_OP_ID,
            first_message_at=datetime(2026, 4, 1, tzinfo=timezone.utc),
            total_user_messages=80,
            days_since_first_contact=46,
            messages_last_7_days=20,
            messages_last_30_days=70,
            longest_session_minutes=42,
            shared_arc_realized_count=1,
            shared_drama_count=0,
            familiarity_band=Familiarity.CLOSE,
            computed_at=datetime(2026, 5, 17, 10, 0, tzinfo=timezone.utc),
        ),
        pending_candidates=(
            CandidateField(
                field_key="diet",
                layer=2,
                proposed_value="不吃辣",
                evidence_ref=_evidence("我不吃辣"),
                raw_extractor_confidence=0.7,
                candidate_id="cand-1",
                character_id=_CHAR_ID,
            ),
        ),
    )


def _build_service():
    persona = _build_persona()
    svc = MagicMock()
    svc.get_current = AsyncMock(return_value=persona)
    svc.render_for_prompt = MagicMock(return_value=["", "關於對方", "- 對方資料：..."])
    svc.invalidate_cache = MagicMock()
    # Owner-checked mutation methods default to "owned → succeeded". Tests
    # that exercise the cross-owner 404 path override the return value.
    svc.get_row_scope = AsyncMock(return_value=(_CHAR_ID, _OP_ID))
    svc.reject_candidate_for_operator = AsyncMock(return_value=True)
    svc.transition_field_state_for_operator = AsyncMock(return_value=True)
    repo = AsyncMock()
    svc._repository = repo
    return svc, repo


def _build_projection_service():
    svc = MagicMock()
    svc.project = AsyncMock(
        return_value=PersonaProjectionResponse(
            character_id=_CHAR_ID,
            narrative="我記得你對科幻電影很有興趣。",
            facts=[
                PersonaProjectionFactResponse(
                    field_id="fld-interests",
                    label="興趣",
                    value="科幻電影",
                ),
            ],
            empty=False,
        ),
    )
    svc.invalidate = MagicMock()
    return svc


def test_get_persona_returns_503_when_service_unavailable():
    client = _client(_Container())
    resp = client.get(f"/api/v1/operator/persona?character_id={_CHAR_ID}")
    assert resp.status_code == 503


def test_get_persona_returns_snapshot():
    svc, _ = _build_service()
    client = _client(_Container(operator_persona_service=svc))
    resp = client.get(f"/api/v1/operator/persona?character_id={_CHAR_ID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["character_id"] == _CHAR_ID
    assert body["operator_id"] == "default"
    # Layer dicts come back as lists sorted by confidence desc.
    layer1_keys = [f["field_key"] for f in body["layer1_identity"]]
    assert layer1_keys == ["name", "occupation"]
    assert body["layer1_identity"][0]["value"] == "丹尼"
    assert body["interaction_strength"]["familiarity_band"] == "close"
    assert body["pending_candidates"][0]["field_key"] == "diet"
    assert "關於對方" in body["prompt_preview_lines"]


def test_get_persona_returns_initial_relationship_summary():
    svc, _ = _build_service()
    repo = AsyncMock()
    repo.get = AsyncMock(
        return_value=CharacterOperatorRelationshipSeed(
            character_id=_CHAR_ID,
            operator_id=_OP_ID,
            relationship_label="老朋友",
            known_context="以前常一起做專案。",
        ),
    )
    client = _client(
        _Container(
            operator_persona_service=svc,
            relationship_seed_repository=repo,
        ),
    )

    resp = client.get(f"/api/v1/operator/persona?character_id={_CHAR_ID}")

    assert resp.status_code == 200
    body = resp.json()
    assert body["initial_relationship"]["relationship_label"] == "老朋友"
    assert "關係：老朋友" in "\n".join(
        body["initial_relationship"]["summary_lines"],
    )
    repo.get.assert_awaited_once_with(_CHAR_ID, _OP_ID)


def test_get_persona_projection_returns_player_safe_shape():
    projection = _build_projection_service()
    client = _client(_Container(operator_persona_projection_service=projection))
    resp = client.get(f"/api/v1/operator/persona/projection?character_id={_CHAR_ID}")
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "character_id": _CHAR_ID,
        "narrative": "我記得你對科幻電影很有興趣。",
        "facts": [
            {
                "field_id": "fld-interests",
                "field_key": "",
                "label": "興趣",
                "value": "科幻電影",
            },
        ],
        "empty": False,
    }
    projection.project.assert_awaited_once_with(_CHAR_ID, operator_id=_OP_ID)


def test_get_persona_projection_returns_503_when_service_unavailable():
    client = _client(_Container())
    resp = client.get(f"/api/v1/operator/persona/projection?character_id={_CHAR_ID}")
    assert resp.status_code == 503


def test_reject_candidate_goes_through_owner_checked_service():
    svc, repo = _build_service()
    client = _client(_Container(operator_persona_service=svc))
    resp = client.post(
        "/api/v1/operator/persona/candidates/cand-1/reject",
    )
    assert resp.status_code == 204
    # The route no longer reaches into the repo directly; it delegates to
    # the owner-checked service method (which scopes by the caller's id).
    svc.reject_candidate_for_operator.assert_awaited_once()
    args = svc.reject_candidate_for_operator.await_args.args
    assert args[0] == "cand-1"
    repo.mark_state.assert_not_awaited()


def test_reject_candidate_not_owned_is_404():
    svc, _repo = _build_service()
    svc.get_row_scope = AsyncMock(return_value=(_CHAR_ID, "someone-else"))
    client = _client(_Container(operator_persona_service=svc))
    resp = client.post(
        "/api/v1/operator/persona/candidates/someone-elses/reject",
    )
    assert resp.status_code == 404
    svc.reject_candidate_for_operator.assert_not_awaited()


def test_transition_field_state_rejects_unknown_state():
    svc, _ = _build_service()
    client = _client(_Container(operator_persona_service=svc))
    resp = client.post(
        "/api/v1/operator/persona/fields/fld-name/state",
        json={"state": "make_it_a_secret"},
    )
    assert resp.status_code == 400


def test_transition_field_state_accepts_stale():
    svc, repo = _build_service()
    projection = _build_projection_service()
    client = _client(
        _Container(
            operator_persona_service=svc,
            operator_persona_projection_service=projection,
        ),
    )
    resp = client.post(
        "/api/v1/operator/persona/fields/fld-name/state",
        json={"state": "stale"},
    )
    assert resp.status_code == 204
    svc.transition_field_state_for_operator.assert_awaited_once()
    args = svc.transition_field_state_for_operator.await_args.args
    assert args[0] == "fld-name"
    assert args[1] == "stale"
    repo.mark_field_state.assert_not_awaited()
    projection.invalidate.assert_called_once_with(_CHAR_ID, _OP_ID)


def test_transition_field_state_not_owned_is_404():
    svc, _repo = _build_service()
    svc.get_row_scope = AsyncMock(return_value=(_CHAR_ID, "someone-else"))
    client = _client(_Container(operator_persona_service=svc))
    resp = client.post(
        "/api/v1/operator/persona/fields/someone-elses/state",
        json={"state": "stale"},
    )
    assert resp.status_code == 404
    svc.transition_field_state_for_operator.assert_not_awaited()


def test_dream_tick_503_when_service_unavailable():
    client = _client(_Container())
    resp = client.post(
        f"/api/v1/admin/operator/persona/dream-tick?character_id={_CHAR_ID}",
    )
    assert resp.status_code == 503


def test_dream_tick_returns_action_counts():
    dream = MagicMock()
    dream.run_consolidation = AsyncMock(
        return_value=ConsolidationResult(
            promotions=[
                PromoteAction(
                    candidate_id="cand-1",
                    field_key="diet",
                    layer=2,
                    value="不吃辣",
                    new_confidence=0.8,
                ),
            ],
        ),
    )
    client = _client(_Container(persona_dream_service=dream))
    resp = client.post(
        f"/api/v1/admin/operator/persona/dream-tick?character_id={_CHAR_ID}",
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied"] is True
    assert body["promotions"] == 1
    assert body["merges"] == 0
