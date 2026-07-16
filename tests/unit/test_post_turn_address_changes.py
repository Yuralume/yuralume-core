"""Post-turn parser extracts typed address_changes (S1 fix).

A chat-time rename like 「叫我森森」 must surface as a typed directional
``AddressChangeSignal`` (so the direction is never inverted by the
first-person memory rewrite), not as a free-text memory.
"""

from __future__ import annotations

from kokoro_link.contracts.post_turn import AddressChangeSignal
from kokoro_link.infrastructure.post_turn.llm_processor import (
    _parse_address_changes,
    _parse_response,
)


def test_parses_player_direction() -> None:
    out = _parse_address_changes([
        {
            "direction": "player",
            "new_value": "森森",
            "subject": "operator_self",
            "old_value": "",
            "source_text": "今天開始叫我森森",
        }
    ])
    assert len(out) == 1
    assert isinstance(out[0], AddressChangeSignal)
    assert out[0].direction == "player"
    assert out[0].new_value == "森森"
    assert out[0].subject == "operator_self"
    assert out[0].source_text == "今天開始叫我森森"


def test_player_direction_requires_operator_self_subject() -> None:
    # A mis-read like 「叫小美過來」 (naming a peer) must NOT land as the
    # operator's own name — same subject discipline as the persona
    # extractor, since this path writes the persona name at 0.95.
    assert _parse_address_changes([
        {"direction": "player", "new_value": "小美", "subject": "character"},
    ]) == []
    # Missing subject is treated conservatively (rejected) for the
    # identity-writing player direction.
    assert _parse_address_changes([
        {"direction": "player", "new_value": "小美"},
    ]) == []


def test_parses_character_direction() -> None:
    out = _parse_address_changes([
        {"direction": "character", "new_value": "小美"}
    ])
    assert len(out) == 1
    assert out[0].direction == "character"
    assert out[0].new_value == "小美"


def test_rejects_unknown_direction() -> None:
    assert _parse_address_changes([{"direction": "both", "new_value": "x"}]) == []
    assert _parse_address_changes([{"new_value": "x"}]) == []


def test_rejects_empty_new_value() -> None:
    assert _parse_address_changes([{"direction": "player", "new_value": ""}]) == []
    assert _parse_address_changes([{"direction": "player"}]) == []


def test_caps_at_two() -> None:
    out = _parse_address_changes([
        {"direction": "player", "new_value": "a", "subject": "operator_self"},
        {"direction": "character", "new_value": "b"},
        {"direction": "player", "new_value": "c", "subject": "operator_self"},
    ])
    assert len(out) == 2


def test_ignores_non_list() -> None:
    assert _parse_address_changes(None) == []
    assert _parse_address_changes("nope") == []


def test_full_response_surfaces_address_change_not_memory() -> None:
    # The model, following the prompt, puts the rename in address_changes
    # and writes no memory for it — the integration point that fixes S1+S3.
    raw = (
        '{"memories": [], "state": {"emotion": "開心"}, '
        '"schedule_adjustments": [], "arc_adjustments": [], '
        '"message_promises": [], '
        '"address_changes": [{"direction": "player", "new_value": "森森", '
        '"subject": "operator_self", "source_text": "今天開始叫我森森"}]}'
    )
    result = _parse_response(
        raw, character_id="c1", conversation_id="conv1",
    )
    assert result.memories == []
    assert len(result.address_changes) == 1
    assert result.address_changes[0].direction == "player"
    assert result.address_changes[0].new_value == "森森"
