"""Post-turn parser extracts message_promises with strict tolerance."""

from __future__ import annotations

from kokoro_link.contracts.post_turn import MessagePromise
from kokoro_link.infrastructure.post_turn.llm_processor import (
    _coerce_iso_datetime,
    _parse_message_promises,
)


def test_iso_datetime_with_time_passes() -> None:
    assert _coerce_iso_datetime("2026-05-18T10:00") == "2026-05-18T10:00"
    assert _coerce_iso_datetime(" 2026-05-18T10:00:30 ") == "2026-05-18T10:00"


def test_iso_datetime_rejects_date_only() -> None:
    # No time component → too vague to act on.
    assert _coerce_iso_datetime("2026-05-18") is None


def test_iso_datetime_rejects_malformed() -> None:
    assert _coerce_iso_datetime("tomorrow at 10") is None
    assert _coerce_iso_datetime("") is None
    assert _coerce_iso_datetime(None) is None


def test_parser_keeps_well_formed_promise() -> None:
    out = _parse_message_promises([
        {
            "scheduled_for_iso": "2026-05-18T10:00",
            "intent": "叫使用者起床",
            "source_text": "明天 10 點叫我起床",
        }
    ])
    assert len(out) == 1
    assert isinstance(out[0], MessagePromise)
    assert out[0].intent == "叫使用者起床"
    assert out[0].source_text == "明天 10 點叫我起床"


def test_parser_drops_missing_time() -> None:
    out = _parse_message_promises([
        {"scheduled_for_iso": "", "intent": "叫起床"},
    ])
    assert out == []


def test_parser_drops_missing_intent() -> None:
    out = _parse_message_promises([
        {"scheduled_for_iso": "2026-05-18T10:00", "intent": ""},
    ])
    assert out == []


def test_parser_caps_at_two_per_turn() -> None:
    raw = [
        {"scheduled_for_iso": f"2026-05-18T{h:02d}:00", "intent": f"事 {h}"}
        for h in (9, 10, 11, 12, 13)
    ]
    out = _parse_message_promises(raw)
    assert len(out) == 2


def test_parser_ignores_non_list() -> None:
    assert _parse_message_promises(None) == []
    assert _parse_message_promises({"not": "a list"}) == []
