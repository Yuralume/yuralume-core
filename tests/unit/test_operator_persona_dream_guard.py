from __future__ import annotations

import json
from datetime import datetime, timezone

from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.operator_persona import OperatorPersona
from kokoro_link.domain.value_objects.profile_field import (
    CandidateField,
    EvidenceRef,
    ProfileField,
)
from kokoro_link.infrastructure.persona.llm_consolidator import (
    _build_prompt,
    _collect_confirmed_fields,
    _parse_response,
)


def _evidence(quote: str = "我是工程師") -> EvidenceRef:
    return EvidenceRef(
        turn_id="msg-1",
        conversation_id="conv-1",
        quote=quote,
        extracted_at=datetime.now(timezone.utc),
    )


def _candidate(
    candidate_id: str,
    *,
    field_key: str = "occupation",
    layer: int = 1,
    value: str = "工程師",
    content_mode: MessageContentMode = MessageContentMode.NORMAL,
) -> CandidateField:
    return CandidateField(
        candidate_id=candidate_id,
        field_key=field_key,
        layer=layer,
        proposed_value=value,
        evidence_ref=_evidence(),
        raw_extractor_confidence=0.8,
        content_mode=content_mode,
        character_id="char-A",
    )


def _field(
    field_id: str,
    *,
    field_key: str = "occupation",
    layer: int = 1,
    value: str = "工程師",
    content_mode: MessageContentMode = MessageContentMode.NORMAL,
) -> ProfileField:
    return ProfileField(
        field_id=field_id,
        field_key=field_key,
        layer=layer,
        value=value,
        confidence=0.9,
        evidence_refs=(_evidence(),),
        last_updated=datetime.now(timezone.utc),
        update_count=1,
        source="extraction",
        content_mode=content_mode,
        character_id="char-A",
    )


def _raw(actions: list[dict]) -> str:
    return json.dumps({"actions": actions}, ensure_ascii=False)


def test_promote_must_match_candidate_layer_and_field_key():
    cand = _candidate("cand-1", field_key="occupation", layer=1)

    result = _parse_response(
        _raw([
            {
                "type": "promote",
                "candidate_id": "cand-1",
                "field_key": "interests",
                "layer": 2,
                "value": "工程師",
                "new_confidence": 0.8,
            },
        ]),
        candidate_by_id={"cand-1": cand},
        valid_field_ids=set(),
        confirmed_by_id={},
    )

    assert result.promotions == []


def test_merge_requires_all_candidates_same_layer_and_key():
    a = _candidate("a", field_key="occupation", layer=1)
    b = _candidate("b", field_key="interests", layer=2)

    result = _parse_response(
        _raw([
            {
                "type": "merge",
                "candidate_ids": ["a", "b"],
                "field_key": "occupation",
                "layer": 1,
                "value": "工程師",
                "new_confidence": 0.8,
            },
        ]),
        candidate_by_id={"a": a, "b": b},
        valid_field_ids=set(),
        confirmed_by_id={},
    )

    assert result.merges == []


def test_supersede_must_match_confirmed_field_and_candidates():
    existing = _field("field-1", field_key="occupation", layer=1)
    c1 = _candidate("c1", field_key="name", layer=1, value="丹尼")
    c2 = _candidate("c2", field_key="name", layer=1, value="丹尼")

    result = _parse_response(
        _raw([
            {
                "type": "supersede",
                "superseded_field_id": "field-1",
                "candidate_ids": ["c1", "c2"],
                "field_key": "name",
                "layer": 1,
                "new_value": "丹尼",
                "new_confidence": 0.9,
                "reason": "new evidence",
            },
        ]),
        candidate_by_id={"c1": c1, "c2": c2},
        valid_field_ids=set(),
        confirmed_by_id={"field-1": existing},
    )

    assert result.supersedes == []


def test_infer_rejects_unknown_or_existing_field_key():
    existing = _field("field-1", field_key="occupation", layer=1)

    result = _parse_response(
        _raw([
            {
                "type": "infer",
                "field_key": "occupation",
                "layer": 1,
                "value": "工程相關工作",
                "new_confidence": 0.5,
                "reason": "already known",
                "supporting_field_ids": ["field-1"],
            },
            {
                "type": "infer",
                "field_key": "made_up_key",
                "layer": 1,
                "value": "x",
                "new_confidence": 0.5,
                "reason": "bad key",
                "supporting_field_ids": ["field-1"],
            },
        ]),
        candidate_by_id={},
        valid_field_ids=set(),
        confirmed_by_id={"field-1": existing},
    )

    assert result.inferences == []


def test_consolidator_prompt_excludes_nsfw_persona_material():
    safe = _field("field-safe", value="工程師")
    sensitive = _field(
        "field-nsfw",
        value="NSFW 偏好",
        content_mode=MessageContentMode.NSFW,
    )
    persona = OperatorPersona(
        character_id="char-A",
        operator_id="default",
        layer1_identity={
            "occupation": safe,
            "interests": sensitive,
        },
    )
    pending = [
        _candidate("cand-safe", value="爵士樂"),
        _candidate(
            "cand-nsfw",
            value="NSFW 候選",
            content_mode=MessageContentMode.NSFW,
        ),
    ]

    prompt = _build_prompt(
        persona=persona,
        pending=pending,
        decay_candidates=[sensitive],
    )
    confirmed = _collect_confirmed_fields(persona)

    assert "工程師" in prompt
    assert "爵士樂" in prompt
    assert "NSFW 偏好" not in prompt
    assert "NSFW 候選" not in prompt
    assert "field-nsfw" not in confirmed
