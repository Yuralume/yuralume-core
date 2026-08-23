from __future__ import annotations

import json

import pytest

from kokoro_link.application.dto.character import InitialRelationshipPayload
from kokoro_link.application.services.character_creation_intake_service import (
    CharacterCreationDraftContext,
    CharacterCreationIntakeService,
)
from kokoro_link.contracts.character_personality_type import (
    CharacterPersonalityTypeAnalysis,
)


class _FakeModel:
    supports_vision = False

    def __init__(self, response: str) -> None:
        self._response = response
        self.last_prompt: str | None = None

    async def generate(self, prompt: str, *, image_urls=None):  # noqa: ARG002
        self.last_prompt = prompt
        return self._response

    def generate_stream(self, prompt: str, *, image_urls=None):  # noqa: ARG002
        async def _empty():
            if False:
                yield ""
        return _empty()


class _CrashingModel:
    supports_vision = False

    async def generate(self, prompt: str, *, image_urls=None):  # noqa: ARG002
        raise RuntimeError("provider exploded")

    def generate_stream(self, prompt: str, *, image_urls=None):  # noqa: ARG002
        async def _empty():
            if False:
                yield ""
        return _empty()


class _FakePersonalityAnalyzer:
    def __init__(self, result: CharacterPersonalityTypeAnalysis) -> None:
        self.result = result
        self.calls: list[object] = []

    async def analyze(self, request):  # noqa: ANN001, ANN201
        self.calls.append(request)
        return self.result


@pytest.mark.asyncio
async def test_analyze_parses_llm_questions_and_caps_to_three() -> None:
    payload = {
        "can_create": False,
        "missing_required": ["known_context", "proactive_cadence_hint", "boundary", "extra"],
        "questions": [
            {"field": "known_context", "question": "你們怎麼認識？", "suggestions": ["第一次見面"]},
            {"field": "user_address_name", "question": "她怎麼稱呼你？"},
            {"field": "proactive_cadence_hint", "question": "多久找你一次？"},
            {"field": "ignored", "question": "不應出現"},
        ],
        "normalized_relationship": {
            "relationship_label": "朋友",
            "living_arrangement": "住在使用者家裡",
        },
        "normalized_user_profile": {"interests": ["音樂"], "life_goals": ["整理作品集"]},
        "warnings": [{"kind": "personality_type_conflict", "message": "類型和人設有點打架"}],
    }
    model = _FakeModel(json.dumps(payload, ensure_ascii=False))
    service = CharacterCreationIntakeService(model=model)

    result = await service.analyze(
        draft=CharacterCreationDraftContext(name="澪"),
        relationship=InitialRelationshipPayload(relationship_label="朋友"),
        current_locale="zh-TW",
    )

    assert result.can_create is False
    # IR4 code-level guarantee, mirrored for the LLM JSON path: even though
    # the model echoed proactive_cadence_hint into missing_required (the
    # old, pre-IR4 habit the prompt tells it not to repeat), the service
    # strips it — cadence must never gate creation.
    assert result.missing_required == ("known_context", "boundary")
    assert [item.field for item in result.questions] == [
        "known_context",
        "user_address_name",
        "proactive_cadence_hint",
    ]
    blocking_by_field = {item.field: item.blocking for item in result.questions}
    assert blocking_by_field["known_context"] is True
    # The model also omitted "blocking" on the cadence question (its old
    # default-to-blocking habit); the service forces it non-blocking rather
    # than trusting the JSON default of True.
    assert blocking_by_field["proactive_cadence_hint"] is False
    assert result.normalized_relationship.relationship_label == "朋友"
    assert result.normalized_relationship.living_arrangement == "住在使用者家裡"
    assert result.normalized_user_profile.interests == ["音樂"]
    assert result.warnings[0].kind == "personality_type_conflict"
    assert "operator" not in (model.last_prompt or "")


@pytest.mark.asyncio
async def test_llm_cadence_question_without_blocking_key_does_not_gate_create() -> None:
    """LLM JSON path counterpart to test_fallback_suggests_cadence_without_blocking_create:
    a model that reverts to its old habit of omitting "blocking" (or setting
    it true) on the cadence question, and/or lists the field in
    missing_required, must still not block character creation — IR4 pinned
    this as advisory-only, and that guarantee cannot depend on prompt
    adherence alone.
    """
    payload = {
        "can_create": True,
        "missing_required": ["proactive_cadence_hint"],
        "questions": [
            {"field": "proactive_cadence_hint", "question": "多久找你一次？"},
        ],
        "normalized_relationship": {},
        "normalized_user_profile": {},
        "warnings": [],
    }
    service = CharacterCreationIntakeService(
        model=_FakeModel(json.dumps(payload, ensure_ascii=False)),
    )

    result = await service.analyze(
        draft=CharacterCreationDraftContext(name="澪"),
        relationship=InitialRelationshipPayload(
            relationship_label="創作夥伴",
            proactive_permission=True,
        ),
    )

    assert result.can_create is True
    assert result.missing_required == ()
    assert [item.field for item in result.questions] == ["proactive_cadence_hint"]
    assert result.questions[0].blocking is False


@pytest.mark.asyncio
async def test_llm_other_blocking_question_still_gates_create() -> None:
    """Counter-example: the cadence-specific override must not leak into
    other fields — a genuinely blocking question (e.g. living_arrangement)
    still withholds can_create.
    """
    payload = {
        "can_create": False,
        "missing_required": ["living_arrangement"],
        "questions": [
            {"field": "living_arrangement", "question": "你們住在一起還是分開住？"},
            {"field": "proactive_cadence_hint", "question": "多久找你一次？", "blocking": True},
        ],
        "normalized_relationship": {},
        "normalized_user_profile": {},
        "warnings": [],
    }
    service = CharacterCreationIntakeService(
        model=_FakeModel(json.dumps(payload, ensure_ascii=False)),
    )

    result = await service.analyze(
        draft=CharacterCreationDraftContext(name="澪"),
        relationship=InitialRelationshipPayload(
            relationship_label="創作夥伴",
            proactive_permission=True,
        ),
    )

    assert result.can_create is False
    assert result.missing_required == ("living_arrangement",)
    blocking_by_field = {item.field: item.blocking for item in result.questions}
    assert blocking_by_field["living_arrangement"] is True
    # Even an explicit "blocking": true from the model is overridden for
    # this one field — cadence is never allowed to gate creation.
    assert blocking_by_field["proactive_cadence_hint"] is False


@pytest.mark.asyncio
async def test_blocking_warning_prevents_create_even_without_questions() -> None:
    payload = {
        "can_create": True,
        "missing_required": [],
        "questions": [],
        "normalized_relationship": {},
        "normalized_user_profile": {},
        "warnings": [
            {
                "kind": "personality_type_conflict",
                "message": "類型和嚴謹人設互相衝突，需要確認。",
                "blocking": True,
            }
        ],
    }
    service = CharacterCreationIntakeService(
        model=_FakeModel(json.dumps(payload, ensure_ascii=False)),
    )

    result = await service.analyze(
        draft=CharacterCreationDraftContext(name="澪"),
        relationship=InitialRelationshipPayload(),
    )

    assert result.can_create is False
    assert result.warnings[0].blocking is True


@pytest.mark.asyncio
async def test_analyze_runs_personality_type_analyzer_and_blocks_conflict() -> None:
    payload = {
        "can_create": True,
        "missing_required": [],
        "questions": [],
        "normalized_relationship": {},
        "normalized_user_profile": {},
        "warnings": [],
    }
    analyzer = _FakePersonalityAnalyzer(
        CharacterPersonalityTypeAnalysis(
            is_consistent=False,
            conflict_level="blocking",
            conflict_notes=("ENTP 和按部就班人設需要確認。",),
            user_questions=("要保留反差，還是改成更重視計畫的類型？",),
        ),
    )
    service = CharacterCreationIntakeService(
        model=_FakeModel(json.dumps(payload, ensure_ascii=False)),
        personality_type_analyzer=analyzer,
    )

    result = await service.analyze(
        draft=CharacterCreationDraftContext(
            name="澪",
            summary="嚴謹、按部就班。",
            personality_type_code="ENTP",
        ),
        relationship=InitialRelationshipPayload(),
        current_locale="zh-TW",
    )

    assert result.can_create is False
    assert analyzer.calls
    assert analyzer.calls[0].user_selected_type.code == "ENTP"
    assert result.warnings[0].blocking is True
    assert result.questions[0].field == "personality_type"


@pytest.mark.asyncio
async def test_fallback_suggests_cadence_without_blocking_create() -> None:
    """IR4: the backend gate now only requires proactive_permission, so a
    blank cadence hint must not withhold can_create — it stays a
    non-blocking nudge (still worth asking; the character otherwise has to
    judge cadence on its own).
    """
    service = CharacterCreationIntakeService(model=_CrashingModel())

    result = await service.analyze(
        draft=CharacterCreationDraftContext(name="澪"),
        relationship=InitialRelationshipPayload(
            relationship_label="創作夥伴",
            known_context="在社群認識，還沒有系統內共同回憶。",
            living_arrangement="分開住",
            proactive_permission=True,
        ),
    )

    assert result.can_create is True
    assert result.missing_required == ()
    assert [question.field for question in result.questions] == [
        "proactive_cadence_hint",
    ]
    assert result.questions[0].blocking is False


@pytest.mark.asyncio
async def test_fallback_still_blocks_on_other_missing_fields_alongside_cadence_nudge() -> None:
    service = CharacterCreationIntakeService(model=_CrashingModel())

    result = await service.analyze(
        draft=CharacterCreationDraftContext(name="澪"),
        relationship=InitialRelationshipPayload(
            relationship_label="創作夥伴",
            known_context="在社群認識，還沒有系統內共同回憶。",
            proactive_permission=True,
        ),
    )

    assert result.can_create is False
    assert result.missing_required == ("living_arrangement",)
    assert [question.field for question in result.questions] == [
        "living_arrangement",
        "proactive_cadence_hint",
    ]
    blocking_by_field = {q.field: q.blocking for q in result.questions}
    assert blocking_by_field["living_arrangement"] is True
    assert blocking_by_field["proactive_cadence_hint"] is False


@pytest.mark.asyncio
async def test_fallback_asks_living_arrangement_for_relationship_intent() -> None:
    service = CharacterCreationIntakeService(model=_CrashingModel())

    result = await service.analyze(
        draft=CharacterCreationDraftContext(name="澪"),
        relationship=InitialRelationshipPayload(
            relationship_label="貼身小精靈",
            known_context="使用者明確設定這是剛創好的角色，還沒有共同回憶。",
        ),
    )

    assert result.can_create is False
    assert result.questions[0].field == "living_arrangement"
    assert "住在一起" in result.questions[0].suggestions


@pytest.mark.asyncio
async def test_blank_relationship_is_create_ready_without_llm_dependency() -> None:
    model = _FakeModel("not json")
    service = CharacterCreationIntakeService(model=model)

    result = await service.analyze(
        draft=CharacterCreationDraftContext(name="澪"),
        relationship=InitialRelationshipPayload(),
    )

    assert result.can_create is True
    assert result.questions == ()
