"""BDD-style tests for the new ``MemoryItem`` participant fields and
the post-turn extractor's parsing of them. Phase 2 of the
world-system roadmap.
"""

from __future__ import annotations

from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.value_objects.actor import ParticipantRef
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.memory_kind import MemoryKind
from kokoro_link.infrastructure.post_turn.llm_processor import (
    _build_prompt,
    _coerce_location,
    _coerce_participants,
)


def test_memory_item_defaults_to_empty_participants_and_no_world():
    item = MemoryItem.create(
        character_id="char-1",
        kind=MemoryKind.SEMANTIC,
        content="角色今天感到平靜",
    )
    assert item.participants == ()
    assert item.world_id is None
    assert item.location is None


def test_memory_item_carries_participants_and_location():
    refs = (
        ParticipantRef(
            actor_kind="operator", actor_id="default", display_name="丹尼",
        ),
        ParticipantRef(
            actor_kind="character", actor_id="char-b", display_name="B",
        ),
    )
    item = MemoryItem.create(
        character_id="char-1",
        kind=MemoryKind.EPISODIC,
        content="B 帶丹尼去吃拉麵",
        participants=refs,
        location="拉麵店",
    )
    assert item.participants == refs
    assert item.location == "拉麵店"


def test_memory_item_strips_blank_location_to_none():
    item = MemoryItem.create(
        character_id="char-1",
        kind=MemoryKind.SEMANTIC,
        content="閒聊",
        location="   ",
    )
    assert item.location is None


def test_coerce_participants_parses_expected_shape():
    refs = _coerce_participants([
        {
            "actor_kind": "operator",
            "actor_id": "default",
            "display_name": "丹尼",
            "role": None,
        },
        {
            "kind": "character",  # alias form
            "id": "char-b",
            "name": "B",
        },
    ])
    assert len(refs) == 2
    assert refs[0].actor_kind == "operator"
    assert refs[0].display_name == "丹尼"
    assert refs[1].actor_kind == "character"
    assert refs[1].display_name == "B"


def test_coerce_participants_drops_malformed_and_unknown_kind():
    refs = _coerce_participants([
        {"actor_kind": "ghost", "display_name": "X"},  # unknown kind → npc
        {"display_name": ""},  # empty name dropped
        "not a dict",
    ])
    # Ghost coerced to npc, blank dropped, string dropped → 1 entry
    assert len(refs) == 1
    assert refs[0].actor_kind == "npc"
    assert refs[0].display_name == "X"


def test_coerce_participants_handles_non_list():
    assert _coerce_participants(None) == ()
    assert _coerce_participants("nope") == ()
    assert _coerce_participants({}) == ()


def test_coerce_location_filters_unknown_sentinels():
    assert _coerce_location("咖啡廳") == "咖啡廳"
    assert _coerce_location("  ") is None
    assert _coerce_location("未知") is None
    assert _coerce_location("unknown") is None
    assert _coerce_location(None) is None


def test_post_turn_prompt_includes_known_peer_context():
    character = Character.create(
        name="小蘭",
        summary="summary",
        personality=[],
        interests=[],
        speaking_style="natural",
        boundaries=[],
        state=CharacterState(
            emotion="neutral",
            affection=50,
            fatigue=0,
            trust=50,
            energy=100,
        ),
    )

    prompt = _build_prompt(
        character=character,
        user_message="你今天有去神社嗎？",
        assistant_message="我有經過一下。",
        recent_messages=[],
        peer_context_lines=[
            "已知角色名冊：",
            "- id=char-b | name=小英 | haunts=神社 | summary=小英在神社打工",
        ],
    )

    assert "已知角色名冊" in prompt
    assert "char-b" in prompt
    assert "小英在神社打工" in prompt
