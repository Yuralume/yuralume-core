"""Typed codecs for the pre-turn snapshots the TU series added.

Sibling of :mod:`turn_snapshot_codec`, which owns the four snapshots the
journal has carried since day one (character state / goals / arc /
schedule). This module owns the ones the completeness work needs —
pending follow-ups, the operator's address preference, and the open 起幕
scene session — and it follows the same three rules:

* **Entities in, entities out.** Every ``*_from_dict`` returns a real
  domain object, never a bag. The undo steps hand the result straight to
  a repository ``save``/``add``, so a field this codec forgets is a field
  the restore silently drops.
* **Plain JSON on the wire.** Timestamps are ISO-8601 strings, value
  objects flatten to their ``.value``. The journal row is a single
  ``payload_json`` Text column, so nothing here may emit a type
  ``json.dumps`` cannot take.
* **Tolerant reads.** A snapshot may have been written by an older
  build; missing optional keys fall back to the entity's own default
  rather than raising, because a journal that cannot be decoded is a
  turn that cannot be undone.

Round-trip is lossless for every field the corresponding restore writes
back — ``tests/unit/test_turn_journal_codec.py`` pins that both ways.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

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
    SCENE_OPEN,
    StorySceneSession,
)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _parse_dt(raw: object) -> datetime | None:
    if not raw:
        return None
    parsed = datetime.fromisoformat(str(raw))
    if parsed.tzinfo is None:
        # Journals are written in UTC; a naive string can only come from
        # a store that dropped the offset. Re-attaching UTC keeps the
        # entity invariants (``scheduled_for`` must be tz-aware) instead
        # of failing the whole restore.
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _require_dt(raw: object, *, field: str) -> datetime:
    parsed = _parse_dt(raw)
    if parsed is None:
        raise ValueError(f"turn journal snapshot is missing {field}")
    return parsed


# ---------- PendingFollowUp ------------------------------------------------


def _follow_up_message_to_dict(
    message: PendingFollowUpMessage,
) -> dict[str, Any]:
    return {
        "content": message.content,
        "queued_at": message.queued_at.isoformat(),
        "content_mode": message.content_mode.value,
        "safe_summary": message.safe_summary,
        "message_id": message.message_id,
    }


def _follow_up_message_from_dict(
    payload: dict[str, Any],
) -> PendingFollowUpMessage:
    # Constructed directly rather than through ``.new`` on purpose: the
    # factory strips and rejects empty content, which is right for a new
    # message and wrong for a replay of one that was already accepted.
    return PendingFollowUpMessage(
        content=str(payload.get("content") or ""),
        queued_at=_require_dt(payload.get("queued_at"), field="queued_at"),
        content_mode=MessageContentMode(
            str(payload.get("content_mode") or MessageContentMode.NORMAL.value),
        ),
        safe_summary=str(payload.get("safe_summary") or ""),
        message_id=payload.get("message_id"),
    )


def follow_up_to_dict(follow_up: PendingFollowUp) -> dict[str, Any]:
    return {
        "id": follow_up.id,
        "character_id": follow_up.character_id,
        "conversation_id": follow_up.conversation_id,
        "status": follow_up.status.value,
        "messages": [
            _follow_up_message_to_dict(m) for m in follow_up.messages
        ],
        "brief_reply": follow_up.brief_reply,
        "defer_reason": follow_up.defer_reason,
        "activity_id": follow_up.activity_id,
        "scheduled_for": follow_up.scheduled_for.isoformat(),
        "queued_at": follow_up.queued_at.isoformat(),
        "updated_at": follow_up.updated_at.isoformat(),
        "resolved_at": _iso(follow_up.resolved_at),
        "resolved_message": follow_up.resolved_message,
        "last_error": follow_up.last_error,
        "kind": follow_up.kind.value,
        "promise_intent": follow_up.promise_intent,
        # Carried so a restore does not blank the anchor of a row an
        # *earlier* turn wrote: the snapshot is written back verbatim,
        # and a row that lost its anchor would fall back to being
        # identified by a time window it does not belong to.
        "turn_record_id": follow_up.turn_record_id,
        # Carried so an undo does not hand the HV1 honesty gate back a
        # full budget on a row that had already spent part of it (F1):
        # the restore step saves this snapshot verbatim, so an omitted
        # counter here is a silent reset to 0 on every undo.
        "honesty_park_attempts": follow_up.honesty_park_attempts,
    }


def follow_up_from_dict(payload: dict[str, Any]) -> PendingFollowUp:
    return PendingFollowUp(
        id=str(payload["id"]),
        character_id=str(payload["character_id"]),
        conversation_id=str(payload["conversation_id"]),
        status=PendingFollowUpStatus(
            str(payload.get("status") or PendingFollowUpStatus.QUEUED.value),
        ),
        messages=tuple(
            _follow_up_message_from_dict(m)
            for m in payload.get("messages") or ()
        ),
        brief_reply=str(payload.get("brief_reply") or ""),
        defer_reason=str(payload.get("defer_reason") or ""),
        activity_id=payload.get("activity_id"),
        scheduled_for=_require_dt(
            payload.get("scheduled_for"), field="scheduled_for",
        ),
        queued_at=_require_dt(payload.get("queued_at"), field="queued_at"),
        updated_at=_require_dt(payload.get("updated_at"), field="updated_at"),
        resolved_at=_parse_dt(payload.get("resolved_at")),
        resolved_message=payload.get("resolved_message"),
        last_error=payload.get("last_error"),
        kind=PendingFollowUpKind(
            str(payload.get("kind") or PendingFollowUpKind.BUSY_DEFER.value),
        ),
        promise_intent=str(payload.get("promise_intent") or ""),
        turn_record_id=(
            str(payload["turn_record_id"])
            if payload.get("turn_record_id") else None
        ),
        # Missing on a snapshot written before F1: treat that as an
        # untouched budget (0), same fallback the entity's own default
        # uses, rather than raising and making the turn un-undoable.
        honesty_park_attempts=int(payload.get("honesty_park_attempts") or 0),
    )


# ---------- OperatorAddressPreference --------------------------------------


def address_preference_to_dict(
    preference: OperatorAddressPreference,
) -> dict[str, Any]:
    return {
        "character_id": preference.character_id,
        "operator_id": preference.operator_id,
        "salutation": preference.salutation,
        "formality_level": preference.formality_level,
        "response_length_pref": preference.response_length_pref,
        "evidence_quote": preference.evidence_quote,
        "updated_at": _iso(preference.updated_at),
    }


def address_preference_from_dict(
    payload: dict[str, Any],
) -> OperatorAddressPreference:
    return OperatorAddressPreference(
        character_id=str(payload["character_id"]),
        operator_id=str(payload["operator_id"]),
        salutation=str(payload.get("salutation") or ""),
        # The entity normalises unknown bands back to its own defaults in
        # ``__post_init__``, so passing the stored string straight through
        # is safe even for a value some later build stops accepting.
        formality_level=str(payload.get("formality_level") or ""),
        response_length_pref=str(payload.get("response_length_pref") or ""),
        evidence_quote=str(payload.get("evidence_quote") or ""),
        updated_at=_parse_dt(payload.get("updated_at")),
    )


# ---------- StorySceneSession ----------------------------------------------


def scene_session_to_dict(session: StorySceneSession) -> dict[str, Any]:
    return {
        "id": session.id,
        "character_id": session.character_id,
        "conversation_id": session.conversation_id,
        "source_layer": session.source_layer,
        "status": session.status,
        "arc_id": session.arc_id,
        "beat_id": session.beat_id,
        "title": session.title,
        "location": session.location,
        "mood": session.mood,
        "scene_type": session.scene_type,
        "dramatic_question": session.dramatic_question,
        "opened_at": session.opened_at.isoformat(),
        "last_activity_at": _iso(session.last_activity_at),
        "closed_at": _iso(session.closed_at),
        "closed_reason": session.closed_reason,
        "operator_position": session.operator_position,
        "operator_note": session.operator_note,
    }


def scene_session_from_dict(payload: dict[str, Any]) -> StorySceneSession:
    return StorySceneSession(
        id=str(payload["id"]),
        character_id=str(payload["character_id"]),
        conversation_id=str(payload["conversation_id"]),
        source_layer=str(payload["source_layer"]),
        status=str(payload.get("status") or SCENE_OPEN),
        arc_id=payload.get("arc_id"),
        beat_id=payload.get("beat_id"),
        title=str(payload.get("title") or ""),
        location=payload.get("location"),
        mood=payload.get("mood"),
        scene_type=payload.get("scene_type"),
        dramatic_question=payload.get("dramatic_question"),
        opened_at=_require_dt(payload.get("opened_at"), field="opened_at"),
        last_activity_at=_parse_dt(payload.get("last_activity_at")),
        closed_at=_parse_dt(payload.get("closed_at")),
        closed_reason=payload.get("closed_reason"),
        operator_position=payload.get("operator_position"),
        operator_note=payload.get("operator_note"),
    )


__all__ = [
    "follow_up_to_dict", "follow_up_from_dict",
    "address_preference_to_dict", "address_preference_from_dict",
    "scene_session_to_dict", "scene_session_from_dict",
]
