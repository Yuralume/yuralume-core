"""Post-turn parser extracts peer_meet_intents conservatively."""

from kokoro_link.contracts.post_turn import PeerMeetIntent
from kokoro_link.infrastructure.post_turn.llm_processor import (
    _parse_response,
)


def test_parser_keeps_well_formed_peer_meet_intent() -> None:
    out = _parse_response(
        """
        {"memories": [], "state": null, "schedule_adjustments": [],
         "arc_adjustments": [], "message_promises": [],
         "peer_meet_intents": [{
           "peer_character_id": "peer-1",
           "peer_name": "小鈴",
           "desired_after_iso": "2026-05-18",
           "topic": "聊明天見面的約定",
           "source_text": "明天去找小鈴"
         }]}
        """,
        character_id="char-1",
        conversation_id="conv-1",
        known_peer_lines=["已知角色名冊：", "- id=peer-1 | name=小鈴"],
    )

    assert len(out.peer_meet_intents) == 1
    assert isinstance(out.peer_meet_intents[0], PeerMeetIntent)
    assert out.peer_meet_intents[0].peer_character_id == "peer-1"
    assert out.peer_meet_intents[0].desired_after_iso == "2026-05-18T00:00"


def test_parser_resolves_peer_name_to_id() -> None:
    out = _parse_response(
        """
        {"memories": [], "state": null, "schedule_adjustments": [],
         "arc_adjustments": [], "message_promises": [],
         "peer_meet_intents": [{
           "peer_name": "小鈴",
           "desired_after_iso": "2026-05-18T13:30",
           "topic": "討論使用者交代的事情"
         }]}
        """,
        character_id="char-1",
        conversation_id="conv-1",
        known_peer_lines=["- id=peer-1 | name=小鈴"],
    )

    assert len(out.peer_meet_intents) == 1
    assert out.peer_meet_intents[0].peer_character_id == "peer-1"


def test_parser_drops_unknown_or_vague_peer_meet_intents() -> None:
    out = _parse_response(
        """
        {"memories": [], "state": null, "schedule_adjustments": [],
         "arc_adjustments": [], "message_promises": [],
         "peer_meet_intents": [
           {"peer_name": "陌生人", "desired_after_iso": "2026-05-18", "topic": "見面"},
           {"peer_character_id": "peer-1", "desired_after_iso": "明天", "topic": "見面"}
         ]}
        """,
        character_id="char-1",
        conversation_id="conv-1",
        known_peer_lines=["- id=peer-1 | name=小鈴"],
    )

    assert out.peer_meet_intents == []
