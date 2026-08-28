"""Schedule backup DTOs (plan §3.1「行程」).

``daily_schedules`` and their ``schedule_activities`` children.
"""

from __future__ import annotations

from datetime import datetime

from kokoro_link.application.dto.character_backup.base import BackupRecord

# Mirrors ``SCHEDULE_UNKNOWN_BUSY_SCORE_DEFAULT`` (infrastructure models)
# without importing infrastructure from the application layer.
_BUSY_SCORE_DEFAULT = 0.4


class DailyScheduleBackupRecord(BackupRecord):
    id: str
    character_id: str
    date: str
    """Civil date string (``YYYY-MM-DD``) exactly as stored."""
    generated_at: datetime
    is_planned: bool = True
    manually_adjusted: bool = False
    """Operator hand-edited this day — exempt from the same-day weather
    re-plan. Defaulted so backups taken before the column exist restore
    as untouched."""
    weather_vet_activity_id: str | None = None
    weather_vet_condition: str | None = None


class ScheduleActivityBackupRecord(BackupRecord):
    id: str
    schedule_id: str
    position: int
    start_at: datetime
    end_at: datetime
    description: str
    category: str
    location: str | None = None
    busy_score: float = _BUSY_SCORE_DEFAULT
    scene_privacy: str | None = None
    meeting_affordance: str | None = None
    source_beat_id: str | None = None
    """KB2 beat lineage — carried so a restored day keeps its reserved
    scene slots out of the memorializer. Defaulted for backups taken
    before the column existed (their blocks read as ordinary activities,
    which is what they were)."""
    memorialized: bool = False
    has_memory: bool = False
    companion_names_json: str = "[]"
    participant_refs_json: str = "[]"
