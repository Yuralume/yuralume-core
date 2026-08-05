"""``arc_templates.beats_json`` codec — the template side of OP0-A.

Template beats live as one JSON blob inside ``ArcTemplateRow``, so the
player-position slot needs no migration here — but it does need the
dump/load pair to carry it, and it needs the same "one bad value must
not cost a beat" stance the SA arc repository has for the column form.

Exercised directly against the private mapping helpers (same shape as
``test_story_arc_persistence_mapping.py``) so the legacy-payload and
malformed-value paths don't have to fight a live engine.
"""

from __future__ import annotations

import json

from kokoro_link.domain.entities.arc_template import ArcTemplateBeat
from kokoro_link.domain.entities.story_arc import (
    OPERATOR_POSITION_CENTRAL,
    OPERATOR_POSITION_PRESENT,
)
from kokoro_link.infrastructure.persistence.sa_arc_template_repository import (
    _dump_beats,
    _load_beats,
)


def _beat(**overrides) -> ArcTemplateBeat:
    defaults = dict(
        sequence=0,
        day_offset=0,
        title="她開口",
        summary="她把話說完之前先深吸了一口氣。",
    )
    defaults.update(overrides)
    return ArcTemplateBeat.create(**defaults)


def test_dump_includes_the_player_position_pair() -> None:
    raw = _dump_beats(
        (
            _beat(
                operator_position=OPERATOR_POSITION_CENTRAL,
                operator_note="她要向你坦白",
            ),
        ),
    )

    payload = json.loads(raw)[0]
    assert payload["operator_position"] == OPERATOR_POSITION_CENTRAL
    assert payload["operator_note"] == "她要向你坦白"


def test_round_trip_preserves_the_pair() -> None:
    beats = (
        _beat(
            sequence=0,
            operator_position=OPERATOR_POSITION_CENTRAL,
            operator_note="她要向你坦白",
        ),
        _beat(sequence=1, day_offset=2, operator_position=OPERATOR_POSITION_PRESENT),
        _beat(sequence=2, day_offset=4),
    )

    restored = _load_beats(_dump_beats(beats))

    assert [b.operator_position for b in restored] == [
        OPERATOR_POSITION_CENTRAL,
        OPERATOR_POSITION_PRESENT,
        None,
    ]
    assert [b.operator_note for b in restored] == ["她要向你坦白", None, None]


def test_unjudged_round_trips_as_none_not_empty_string() -> None:
    restored = _load_beats(_dump_beats((_beat(),)))

    assert restored[0].operator_position is None
    assert restored[0].operator_note is None


def test_legacy_payload_without_the_keys_loads_as_unjudged() -> None:
    """Rows written before OP0-A simply lack the keys."""
    raw = json.dumps(
        [
            {
                "sequence": 0,
                "day_offset": 0,
                "title": "舊 beat",
                "summary": "summary",
                "tension": "setup",
                "scene_type": "encounter",
                "location": None,
                "scene_characters": [],
                "dramatic_question": None,
                "required": True,
            },
        ],
    )

    restored = _load_beats(raw)

    assert len(restored) == 1
    assert restored[0].operator_position is None
    assert restored[0].operator_note is None


def test_unreadable_position_costs_the_hint_not_the_beat() -> None:
    """``_load_beats`` drops a beat whose ``create`` raises, so an
    off-vocabulary position has to be neutralised on the way in — losing
    a framing hint is recoverable, losing a scene is not."""
    raw = _dump_beats((_beat(operator_note="她要向你坦白"),))
    payload = json.loads(raw)
    payload[0]["operator_position"] = "protagonist"

    restored = _load_beats(json.dumps(payload))

    assert len(restored) == 1
    assert restored[0].operator_position is None
    assert restored[0].operator_note == "她要向你坦白"
