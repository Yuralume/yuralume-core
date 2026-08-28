"""Conversation-domain backup DTOs (plan §3.1「對話」).

``conversations`` / ``messages`` / ``turn_journals`` /
``pending_follow_ups`` / ``deferred_intents`` / ``dialogue_checkpoints``.
"""

from __future__ import annotations

from datetime import datetime

from kokoro_link.application.dto.character_backup.base import BackupRecord


class ConversationBackupRecord(BackupRecord):
    id: str
    character_id: str
    source: str = "web"
    version: int = 1


class MessageBackupRecord(BackupRecord):
    """``messages`` row.

    ``id`` is the exporter's autoincrement surrogate — carried so a
    restore can build an id remap for payloads that reference message
    row ids (e.g. turn-journal snapshots); position identity within a
    conversation is ``(conversation_id, position)``. ``content_mode`` /
    ``safe_summary`` are carried verbatim: the hosted NSFW collapse (D5)
    happens at import time, never at the format layer.
    """

    id: int
    conversation_id: str
    position: int
    role: str
    content: str
    kind: str = "chat"
    attachments_json: str = "[]"
    created_at: datetime
    content_mode: str = "normal"
    safe_summary: str = ""
    idempotency_key: str | None = None


class TurnJournalBackupRecord(BackupRecord):
    id: str
    conversation_id: str
    character_id: str
    turn_index: int
    created_at: datetime
    payload_json: str = "{}"


class PendingFollowUpBackupRecord(BackupRecord):
    id: str
    character_id: str
    conversation_id: str
    status: str = "queued"
    activity_id: str | None = None
    brief_reply: str = ""
    defer_reason: str = ""
    messages_json: str = "[]"
    scheduled_for: datetime
    queued_at: datetime
    updated_at: datetime
    resolved_at: datetime | None = None
    resolved_message: str | None = None
    last_error: str | None = None
    kind: str = "busy_defer"
    promise_intent: str = ""
    turn_record_id: str | None = None
    """Added after v1 — defaults to ``None`` so an archive written
    before the column existed still restores (base.py §version policy).

    Carried rather than dropped because ``turn_journals`` is carried too,
    and the anchor is only meaningful as a *pair*: the journal's
    ``turn_record_id`` (inside its ``payload_json``) against the row's.
    Drop one side and a restored archive's undo would fall back to
    identifying promise rows by time window — the failure this column
    exists to remove. Neither side is remapped on import, so the pairing
    survives verbatim; the ``turn_records`` row itself is telemetry and
    is not carried, which costs nothing because nothing reads it here."""
    honesty_park_attempts: int = 0
    """Added after v1 — defaults to ``0`` so an archive written before the
    column existed still restores (base.py §version policy).

    Carried rather than reset because it is the retry budget of a promise
    that is itself carried: restoring a row whose model has already lied
    through most of its allowance with a fresh full allowance would hand
    the restored character back the exact loop the budget exists to end."""


class DeferredIntentBackupRecord(BackupRecord):
    id: str
    character_id: str
    operator_id: str
    trigger: str = "tick"
    inner_motive: str = ""
    conversation_purpose: str = ""
    expected_reply: str = ""
    risk: str = ""
    best_timing: str = ""
    reason: str = ""
    status: str = "active"
    created_at: datetime
    expires_at: datetime
    consumed_at: datetime | None = None
    revisit_at: datetime | None = None
    """Added after v1 — defaults to ``None`` so an archive written
    before the column existed still restores (base.py §version
    tolerance)."""


class DialogueCheckpointBackupRecord(BackupRecord):
    """``dialogue_checkpoints`` — the pair's cumulative dialogue summary.

    Carried rather than left to rebuild itself (DH3). The checkpoint is
    the only place the character's memory of everything older than the
    loaded window still exists: the messages behind it are in the
    archive, but the *summary* of them is not derivable from a window
    that no longer reaches back that far. Dropping it would restore a
    character who has forgotten the relationship — visibly, and with no
    way to get it back.

    ``covers_until_message_key`` is a content fingerprint of a message,
    not a row id, so it survives the import's id reissue untouched and
    still names the same message on the far side.
    """

    character_id: str
    operator_id: str
    summary_text: str = ""
    covers_until_message_key: str
    covers_until_created_at: datetime
    updated_at: datetime
    model: str = ""
    stale: bool = False
