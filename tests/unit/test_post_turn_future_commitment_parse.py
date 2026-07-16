"""Post-turn parser extracts target_date_iso for future-add commitments."""

from __future__ import annotations

from kokoro_link.infrastructure.post_turn.llm_processor import (
    _coerce_iso_date,
    _parse_adjustments,
)


def test_iso_date_passthrough() -> None:
    assert _coerce_iso_date("2026-05-20") == "2026-05-20"
    assert _coerce_iso_date("  2026-05-20 ") == "2026-05-20"


def test_iso_date_rejects_malformed() -> None:
    assert _coerce_iso_date("tomorrow") is None
    assert _coerce_iso_date("2026/05/20") is None
    assert _coerce_iso_date("") is None
    assert _coerce_iso_date(None) is None


def test_parser_keeps_target_date_for_add() -> None:
    payload = [
        {
            "action": "add",
            "start": "19:00",
            "end": "21:00",
            "description": "看電影",
            "category": "leisure",
            "target_date_iso": "2026-05-20",
        },
    ]
    out = _parse_adjustments(payload, known_activity_ids=set())
    assert len(out) == 1
    assert out[0].target_date_iso == "2026-05-20"
    assert out[0].action == "add"


def test_parser_strips_target_date_for_remove() -> None:
    """remove/modify never carry a future date — they act on
    today-existing activities by id."""
    payload = [
        {
            "action": "remove",
            "activity_id": "ghost",
            "target_date_iso": "2026-05-20",
        },
    ]
    # known set is empty, so this gets dropped anyway. Use modify with
    # an id present in the known set to exercise the strip path.
    payload2 = [
        {
            "action": "modify",
            "activity_id": "real-id",
            "description": "改個描述",
            "target_date_iso": "2026-05-20",
        },
    ]
    out = _parse_adjustments(payload2, known_activity_ids={"real-id"})
    assert len(out) == 1
    assert out[0].target_date_iso is None


def test_parser_drops_malformed_target_date() -> None:
    payload = [
        {
            "action": "add",
            "start": "19:00",
            "end": "21:00",
            "description": "看電影",
            "category": "leisure",
            "target_date_iso": "tomorrow",
        },
    ]
    out = _parse_adjustments(payload, known_activity_ids=set())
    # The adjustment still goes through (today-bucket); the malformed
    # date is normalised to None and ScheduleService falls back.
    assert len(out) == 1
    assert out[0].target_date_iso is None
