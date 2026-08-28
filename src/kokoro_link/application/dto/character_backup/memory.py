"""Memory-domain backup DTOs (plan §3.1「記憶」).

``memory_items`` (text + metadata only) and ``memoir_pins``.
"""

from __future__ import annotations

from datetime import datetime

from kokoro_link.application.dto.character_backup.base import BackupRecord


class MemoryItemBackupRecord(BackupRecord):
    """``memory_items`` minus the two ``Vector(1024)`` columns.

    ``embedding`` / ``tags_embedding`` are classified RECOMPUTE (§3.2):
    the restore job re-derives them through the existing
    ``attach_embeddings`` path. Vectors carry no model tag, so copying
    them across instances with different embedding models would silently
    degrade retrieval — always recomputing sidesteps that entirely.
    """

    id: str
    character_id: str
    conversation_id: str | None = None
    kind: str
    content: str
    salience: float = 0.5
    tags: str = "[]"
    created_at: datetime
    last_accessed_at: datetime | None = None
    access_count: int = 0
    participants_json: str = "[]"
    world_id: str | None = None
    location: str | None = None
    audience: str = ""
    player_knowledge: str | None = None


class MemoirPinBackupRecord(BackupRecord):
    id: str
    character_id: str
    operator_id: str
    entry_kind: str
    entry_id: str
    pinned_at: datetime
    created_at: datetime
