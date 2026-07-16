"""Unit tests for the LLM extractor's anti-hallucination guards.

These don't exercise the LLM — we feed the parser canned JSON outputs
and check that the guard logic in :func:`_parse_response` keeps the
hallucinated rows out and the well-formed rows in.
"""

from __future__ import annotations

import json

from kokoro_link.infrastructure.persona.llm_extractor import _parse_response


def _wrap(candidates: list[dict]) -> str:
    return json.dumps({"candidates": candidates})


def test_substring_guard_drops_quote_not_in_user_text():
    raw = _wrap([
        {
            "layer": 1,
            "field_key": "occupation",
            "value": "engineer",
            "quote": "我住在火星",
            "self_confidence": 0.9,
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="t",
        user_message="今天的天氣不錯。",
        recent_user_messages=("早上好",),
    )
    assert result == []


def test_recent_history_quote_is_context_only_not_evidence():
    raw = _wrap([
        {
            "layer": 1,
            "field_key": "occupation",
            "value": "engineer",
            "quote": "我是工程師",
            "self_confidence": 0.9,
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="t-new",
        user_message="今天只是來打招呼。",
        recent_user_messages=("我是工程師",),
    )
    assert result == []


def test_low_self_confidence_dropped():
    raw = _wrap([
        {
            "layer": 1,
            "field_key": "occupation",
            "value": "engineer",
            "quote": "我是工程師",
            "self_confidence": 0.3,
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="t",
        user_message="我是工程師",
        recent_user_messages=(),
    )
    assert result == []


def test_layer_5_without_explicit_flag_dropped():
    """Layer 5 (trust) requires explicit=true; without it the extractor
    must NOT stage anything even when a substring match exists."""
    raw = _wrap([
        {
            "layer": 5,
            "field_key": "money_borrowed",
            "value": "borrowed 5000",
            "quote": "上次借的五千",
            "self_confidence": 0.9,
            "explicit": False,
            "subject": "operator_self",
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="t",
        user_message="上次借的五千下週還",
        recent_user_messages=(),
    )
    assert result == []


def test_layer_4_silently_rejected():
    """Layer 4 is computed; the extractor must NEVER persist it."""
    raw = _wrap([
        {
            "layer": 4,
            "field_key": "messages_last_7_days",
            "value": "80",
            "quote": "I message you a lot",
            "self_confidence": 0.95,
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="t",
        user_message="I message you a lot",
        recent_user_messages=(),
    )
    assert result == []


def test_unknown_field_key_rejected():
    raw = _wrap([
        {
            "layer": 1,
            "field_key": "favourite_pokemon",  # not in the layer dict
            "value": "pikachu",
            "quote": "皮卡丘",
            "self_confidence": 0.9,
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="t",
        user_message="我最喜歡皮卡丘",
        recent_user_messages=(),
    )
    assert result == []


def test_well_formed_candidate_passes_all_guards():
    raw = _wrap([
        {
            "layer": 1,
            "field_key": "occupation",
            "value": "後端工程師",
            "quote": "我是後端工程師",
            "self_confidence": 0.85,
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="msg-7",
        user_message="我是後端工程師，常常加班。",
        recent_user_messages=(),
    )
    assert len(result) == 1
    candidate = result[0]
    assert candidate.character_id == "char-A"
    assert candidate.field_key == "occupation"
    assert candidate.layer == 1
    assert candidate.proposed_value == "後端工程師"
    assert candidate.evidence_ref.quote == "我是後端工程師"
    assert candidate.evidence_ref.turn_id == "msg-7"
    assert candidate.state == "pending"
    assert candidate.source == "extraction"


def test_explicit_layer_5_passes_and_marks_user_explicit_source():
    raw = _wrap([
        {
            "layer": 5,
            "field_key": "money_borrowed",
            "value": "5000",
            "quote": "上次跟你借的五千下週還",
            "self_confidence": 0.95,
            "explicit": True,
            "subject": "operator_self",
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="msg-9",
        user_message="上次跟你借的五千下週還喔",
        recent_user_messages=(),
    )
    assert len(result) == 1
    assert result[0].source == "user_explicit"
    assert result[0].explicit is True


def test_layer3_requires_operator_self_subject():
    raw = _wrap([
        {
            "layer": 3,
            "field_key": "anxieties",
            "value": "朋友很焦慮",
            "quote": "我朋友最近很焦慮",
            "self_confidence": 0.9,
            "subject": "other_person",
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="msg-10",
        user_message="我朋友最近很焦慮",
        recent_user_messages=(),
    )
    assert result == []


def test_layer3_operator_self_subject_passes():
    raw = _wrap([
        {
            "layer": 3,
            "field_key": "anxieties",
            "value": "擔心被否定",
            "quote": "我其實很怕被否定",
            "self_confidence": 0.9,
            "subject": "operator_self",
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="msg-11",
        user_message="我其實很怕被否定",
        recent_user_messages=(),
    )
    assert len(result) == 1
    assert result[0].field_key == "anxieties"


def test_layer1_name_with_peer_subject_dropped():
    """A peer character the user merely mentions must NOT become the
    user's own Layer-1 name — the subject!=operator_self guard now covers
    identity fields, so the persona/memoir never shows a peer's name as
    the player's name."""
    raw = _wrap([
        {
            "layer": 1,
            "field_key": "name",
            "value": "角色B",
            "quote": "我跟角色B很熟",
            "self_confidence": 0.9,
            "subject": "character",
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="msg-12",
        user_message="我跟角色B很熟",
        recent_user_messages=(),
    )
    assert result == []


def test_layer1_nickname_without_subject_dropped():
    """Identity naming is strict: a name/nickname candidate with no clear
    operator_self attribution is rejected (a name we cannot attribute to
    the operator must not become the operator's name)."""
    raw = _wrap([
        {
            "layer": 1,
            "field_key": "nickname",
            "value": "小森",
            "quote": "大家都叫小森",
            "self_confidence": 0.9,
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="msg-13",
        user_message="大家都叫小森",
        recent_user_messages=(),
    )
    assert result == []


def test_layer1_name_with_operator_self_passes():
    raw = _wrap([
        {
            "layer": 1,
            "field_key": "name",
            "value": "森森",
            "quote": "叫我森森就好",
            "self_confidence": 0.9,
            "subject": "operator_self",
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="msg-14",
        user_message="以後叫我森森就好",
        recent_user_messages=(),
    )
    assert len(result) == 1
    assert result[0].field_key == "name"
    assert result[0].proposed_value == "森森"


def test_layer1_occupation_with_peer_subject_dropped():
    """The third-party reject also covers ordinary descriptive Layer-1
    fields when the model explicitly marks a non-self subject."""
    raw = _wrap([
        {
            "layer": 1,
            "field_key": "occupation",
            "value": "醫生",
            "quote": "我朋友是醫生",
            "self_confidence": 0.9,
            "subject": "other_person",
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="msg-15",
        user_message="我朋友是醫生",
        recent_user_messages=(),
    )
    assert result == []


def test_layer1_descriptive_unclear_subject_dropped():
    """A descriptive fact the model can't attribute to the operator
    (subject=unclear) is not banked as the operator's own fact."""
    raw = _wrap([
        {
            "layer": 1,
            "field_key": "occupation",
            "value": "醫生",
            "quote": "醫生最近很忙",
            "self_confidence": 0.9,
            "subject": "unclear",
        },
    ])
    result = _parse_response(
        raw,
        character_id="char-A",
        conversation_id="conv-1",
        user_message_id="msg-16",
        user_message="醫生最近很忙",
        recent_user_messages=(),
    )
    assert result == []


def test_malformed_json_returns_empty():
    assert _parse_response(
        "not json at all",
        character_id="char-A",
        conversation_id="c",
        user_message_id="t",
        user_message="anything",
        recent_user_messages=(),
    ) == []
