"""Round-trip pins for the TU1 journal snapshot codec.

The journal is a single JSON blob and the undo steps hand whatever comes
back out of it straight to a repository ``save``. A field this codec
drops is therefore not a decoding bug the caller notices — it is a
restore that quietly puts back an incomplete row. So each snapshot is
asserted **entity-in, equal-entity-out**, not field-by-field: a new
attribute on any of these entities fails here until the codec learns it.

The second half pins the two places where ``None`` carries meaning:

* ``had_active_arc`` is tri-state. ``False`` (there was definitely no
  arc) must not come back as ``None`` (we never found out) — ``False``
  is the only value that licenses deleting an arc the turn created.
* ``turn_record_id`` is absent for a busy-defer turn by design, and the
  stamping helper must not blank an id once it is set.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from kokoro_link.application.services.turn_journal_snapshots import (
    address_preference_from_dict,
    address_preference_to_dict,
    follow_up_from_dict,
    follow_up_to_dict,
    scene_session_from_dict,
    scene_session_to_dict,
)
from kokoro_link.domain.entities.conversation import MessageContentMode
from kokoro_link.domain.entities.operator_address_preference import (
    OperatorAddressPreference,
)
from kokoro_link.domain.entities.pending_follow_up import (
    PendingFollowUp,
    PendingFollowUpKind,
    PendingFollowUpMessage,
    PendingFollowUpStatus,
)
from kokoro_link.domain.entities.story_scene_session import (
    SCENE_CLOSE_RESOLVED,
    SCENE_CLOSED,
    SCENE_LAYER_BEAT,
    SCENE_OPEN,
    StorySceneSession,
)
from kokoro_link.domain.entities.turn_journal import TurnJournal
from kokoro_link.infrastructure.persistence.models import TurnJournalRow
from kokoro_link.infrastructure.persistence.sa_turn_journal_repository import (
    _journal_to_payload,
    _row_to_domain,
)

_NOW = datetime(2026, 8, 25, 9, 30, tzinfo=timezone.utc)


def _follow_up() -> PendingFollowUp:
    return PendingFollowUp(
        id="follow-1",
        character_id="char-1",
        conversation_id="conv-1",
        status=PendingFollowUpStatus.QUEUED,
        messages=(
            PendingFollowUpMessage(
                content="在忙嗎",
                queued_at=_NOW,
                content_mode=MessageContentMode.NORMAL,
                safe_summary="asking if busy",
                message_id="msg-1",
            ),
            PendingFollowUpMessage(
                content="沒事了",
                queued_at=_NOW + timedelta(minutes=2),
                content_mode=MessageContentMode.NORMAL,
            ),
        ),
        brief_reply="等等回你",
        defer_reason="會議中",
        activity_id="activity-1",
        scheduled_for=_NOW + timedelta(hours=1),
        queued_at=_NOW,
        updated_at=_NOW + timedelta(minutes=2),
        resolved_at=None,
        resolved_message=None,
        last_error=None,
        kind=PendingFollowUpKind.BUSY_DEFER,
        promise_intent="",
    )


def _scene_session(**overrides) -> StorySceneSession:  # noqa: ANN003
    base = dict(
        id="scene-1",
        character_id="char-1",
        conversation_id="conv-1",
        source_layer=SCENE_LAYER_BEAT,
        status=SCENE_OPEN,
        arc_id="arc-1",
        beat_id="beat-1",
        title="雨天的琴房",
        location="音樂教室",
        mood="安靜",
        scene_type="encounter",
        dramatic_question="她會說出口嗎",
        opened_at=_NOW,
        last_activity_at=_NOW + timedelta(minutes=5),
        operator_position="present",
        operator_note="玩家坐在窗邊",
    )
    base.update(overrides)
    return StorySceneSession(**base)


def _preference() -> OperatorAddressPreference:
    return OperatorAddressPreference(
        character_id="char-1",
        operator_id="user-1",
        salutation="森森",
        formality_level="low",
        response_length_pref="long",
        evidence_quote="叫我森森就好",
        updated_at=_NOW,
    )


def _assert_json_safe(payload: dict) -> None:
    """A snapshot that ``json.dumps`` cannot take would only fail at
    persist time, in a fail-soft ``except`` that logs and moves on — i.e.
    the turn would silently become un-undoable."""
    json.dumps(payload, ensure_ascii=False)


# ---------- entity round trips ---------------------------------------------


def test_pending_follow_up_round_trips_whole() -> None:
    original = _follow_up()
    payload = follow_up_to_dict(original)
    _assert_json_safe(payload)

    assert follow_up_from_dict(payload) == original


def test_pending_follow_up_round_trips_honesty_park_attempts() -> None:
    """A row the HV1 honesty gate has already parked twice must not come
    back from an undo with its budget silently reset to 0 (FX1) — the
    restore step saves this snapshot verbatim, so a codec gap here is a
    free retry the model earned by re-claiming an unevidenced outcome."""
    original = replace(_follow_up(), honesty_park_attempts=2)
    payload = follow_up_to_dict(original)
    _assert_json_safe(payload)
    assert payload["honesty_park_attempts"] == 2

    assert follow_up_from_dict(payload) == original


def test_pending_follow_up_round_trips_a_scheduled_promise() -> None:
    """The promise variant carries the fields busy-defer leaves empty —
    a codec written against only the defer shape loses the intent that
    is the whole content of the eventual message."""
    original = replace(
        _follow_up(),
        kind=PendingFollowUpKind.SCHEDULED_PROMISE,
        promise_intent="早上十點叫玩家起床",
        brief_reply="",
        resolved_at=_NOW + timedelta(hours=2),
        resolved_message="起床囉",
        last_error="upstream timeout",
        status=PendingFollowUpStatus.RESOLVED,
        turn_record_id="turn-9",
    )

    assert follow_up_from_dict(follow_up_to_dict(original)) == original


def test_pending_follow_up_round_trips_the_turn_anchor() -> None:
    """The anchor has to survive the snapshot, because a restore writes
    the snapshot back verbatim.

    A promise row that came back anchorless would be claimed by the next
    undo's time window — which is precisely the misattribution the
    anchor exists to end. And a payload written before the field existed
    has to decode to ``None`` rather than raise: those journals are on
    disk in every deployment."""
    anchored = replace(_follow_up(), turn_record_id="turn-9")
    payload = follow_up_to_dict(anchored)
    _assert_json_safe(payload)
    assert payload["turn_record_id"] == "turn-9"
    assert follow_up_from_dict(payload) == anchored

    legacy = follow_up_to_dict(_follow_up())
    legacy.pop("turn_record_id")
    assert follow_up_from_dict(legacy).turn_record_id is None


def test_address_preference_round_trips_whole() -> None:
    original = _preference()
    payload = address_preference_to_dict(original)
    _assert_json_safe(payload)

    assert address_preference_from_dict(payload) == original


def test_open_scene_session_round_trips_whole() -> None:
    original = _scene_session()
    payload = scene_session_to_dict(original)
    _assert_json_safe(payload)

    assert scene_session_from_dict(payload) == original


def test_closed_scene_session_round_trips_its_close(  # noqa: D103
) -> None:
    original = _scene_session(
        status=SCENE_CLOSED,
        closed_at=_NOW + timedelta(minutes=40),
        closed_reason=SCENE_CLOSE_RESOLVED,
    )

    restored = scene_session_from_dict(scene_session_to_dict(original))

    assert restored == original
    # The reason the close has to survive: TU5 re-opens by writing the
    # *pre-turn* row back, and the entity rejects an open session that
    # still carries a close reason — so a codec that dropped these two
    # would make the difference between the two states invisible.
    assert restored.closed_reason == SCENE_CLOSE_RESOLVED


def test_naive_timestamps_are_read_back_as_utc() -> None:
    """A store that drops the offset must not produce an entity that
    raises on the next comparison (``scheduled_for`` is required to be
    tz-aware)."""
    payload = follow_up_to_dict(_follow_up())
    payload["scheduled_for"] = "2026-08-25T10:30:00"

    restored = follow_up_from_dict(payload)

    assert restored.scheduled_for.tzinfo is not None
    assert restored.scheduled_for == datetime(
        2026, 8, 25, 10, 30, tzinfo=timezone.utc,
    )


def test_snapshot_written_before_optional_fields_existed_still_decodes() -> None:
    """Journals live at most 5 per conversation but can outlive a
    deploy; a missing optional key falls back to the entity default
    instead of raising and making the turn un-undoable."""
    payload = follow_up_to_dict(_follow_up())
    for legacy_gap in (
        "kind", "promise_intent", "last_error", "activity_id",
        "honesty_park_attempts",
    ):
        payload.pop(legacy_gap)

    restored = follow_up_from_dict(payload)

    assert restored.kind == PendingFollowUpKind.BUSY_DEFER
    assert restored.promise_intent == ""
    assert restored.activity_id is None
    assert restored.honesty_park_attempts == 0


# ---------- the journal payload itself -------------------------------------


def _journal(**overrides) -> TurnJournal:  # noqa: ANN003
    base = dict(
        conversation_id="conv-1",
        character_id="char-1",
        turn_index=4,
        turn_started_at=_NOW,
        prev_character_state={"emotion": "neutral", "affection": 50},
        prev_goals=[],
        prev_active_arc=None,
        prev_daily_schedule=None,
        had_active_arc=False,
        prev_open_follow_ups=[follow_up_to_dict(_follow_up())],
        prev_address_preference=address_preference_to_dict(_preference()),
        prev_scene_session=scene_session_to_dict(_scene_session()),
    )
    base.update(overrides)
    return TurnJournal.new(**base)


def _round_trip(journal: TurnJournal) -> TurnJournal:
    payload = _journal_to_payload(journal)
    _assert_json_safe(payload)
    row = TurnJournalRow(
        id=journal.id,
        conversation_id=journal.conversation_id,
        character_id=journal.character_id,
        turn_index=journal.turn_index,
        created_at=journal.created_at,
        payload_json=json.dumps(payload, ensure_ascii=False),
    )
    return _row_to_domain(row)


def test_journal_payload_carries_every_new_snapshot() -> None:
    journal = _journal().with_turn_record_id("turn-record-1")

    restored = _round_trip(journal)

    assert restored.turn_record_id == "turn-record-1"
    assert restored.had_active_arc is False
    assert restored.prev_open_follow_ups == journal.prev_open_follow_ups
    assert restored.prev_address_preference == journal.prev_address_preference
    assert restored.prev_scene_session == journal.prev_scene_session
    # And the snapshots decode back into real entities from there.
    assert (
        follow_up_from_dict(restored.prev_open_follow_ups[0]) == _follow_up()
    )
    assert scene_session_from_dict(restored.prev_scene_session) == (
        _scene_session()
    )


@pytest.mark.parametrize("captured", [True, False, None])
def test_had_active_arc_keeps_all_three_states(captured: bool | None) -> None:
    """``False`` must not collapse into ``None``. ``False`` says "there
    was definitely no arc" and is the only value that lets undo delete
    an arc the turn created; ``None`` says "never found out" and must
    keep undo's hands off."""
    restored = _round_trip(_journal(had_active_arc=captured))

    assert restored.had_active_arc is captured


def test_journal_written_before_tu1_decodes_with_the_timid_defaults() -> None:
    """An old row has none of the new keys. It must still load, and it
    must load into the state that makes every new step do nothing."""
    row = TurnJournalRow(
        id="journal-legacy",
        conversation_id="conv-1",
        character_id="char-1",
        turn_index=2,
        created_at=_NOW,
        payload_json=json.dumps({
            "turn_started_at": _NOW.isoformat(),
            "prev_character_state": {"emotion": "neutral"},
            "prev_goals": [],
            "prev_active_arc": None,
            "prev_daily_schedule": None,
        }),
    )

    restored = _row_to_domain(row)

    assert restored.turn_record_id is None
    assert restored.had_active_arc is None
    assert restored.prev_open_follow_ups == []
    assert restored.prev_address_preference is None
    assert restored.prev_scene_session is None


# ---------- turn_record_id stamping ----------------------------------------


def test_turn_record_id_is_stamped_after_the_fact() -> None:
    journal = _journal()
    assert journal.turn_record_id is None

    stamped = journal.with_turn_record_id("turn-record-1")

    assert stamped.turn_record_id == "turn-record-1"
    # Frozen entity: stamping returns a copy, it does not mutate.
    assert journal.turn_record_id is None
    assert stamped.id == journal.id


def test_stamping_none_leaves_the_journal_alone() -> None:
    """The busy-defer branch runs no post-turn and mints no turn record.
    That path must produce a journal with no anchor — and must not blank
    an anchor that some other path already set."""
    unstamped = _journal()
    assert unstamped.with_turn_record_id(None) is unstamped

    stamped = unstamped.with_turn_record_id("turn-record-1")
    assert stamped.with_turn_record_id(None) is stamped
    assert stamped.with_turn_record_id("turn-record-1") is stamped
