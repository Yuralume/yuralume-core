"""Conversation-domain backup DTOs (plan §3.1「對話」).

``conversations`` / ``messages`` / ``turn_journals`` /
``pending_follow_ups`` / ``deferred_intents``.
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
