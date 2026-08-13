"""State / portrait backup DTOs (plan §3.1「狀態與畫像」, character-only half).

``state_snapshots`` / ``character_goals`` / ``emotion_events`` /
``disposition_drift_history`` / ``self_reflections`` /
``behavioral_patterns``. The operator-persona half lives in
``persona.py``.
"""

from __future__ import annotations

from datetime import date, datetime

from kokoro_link.application.dto.character_backup.base import BackupRecord


class StateSnapshotBackupRecord(BackupRecord):
    id: str
    character_id: str
    source: str
    emotion: str
    affection: int
    fatigue: int
    trust: int
    energy: int
    created_at: datetime
    trigger: str | None = None


class CharacterGoalBackupRecord(BackupRecord):
    id: str
    character_id: str
    content: str
    status: str = "active"
    priority: int = 3
    origin: str = "manual"
    tags: str = "[]"
    created_at: datetime
    last_progressed_at: datetime | None = None
    review_notes: str | None = None


class EmotionEventBackupRecord(BackupRecord):
    id: str
    character_id: str
    operator_id: str
    cause_ref_kind: str
    cause_ref_id: str | None = None
    valence: float = 0.0
    arousal: float = 0.0
    intensity: float = 0.5
    affection_delta: int = 0
    fatigue_delta: int = 0
    trust_delta: int = 0
    energy_delta: int = 0
    applied_to_state: bool = False
    emotion_label: str = ""
    evidence_quote: str = ""
    decay_half_life_minutes: int = 240
    expires_at: datetime | None = None
    created_at: datetime


class DispositionDriftHistoryBackupRecord(BackupRecord):
    id: str
    character_id: str
    dimension: str
    from_band: str
    to_band: str
    reason: str = ""
    evidence_quote: str = ""
    decided_at: datetime


class SelfReflectionBackupRecord(BackupRecord):
    id: str
    character_id: str
    operator_id: str
    period: str
    narrative: str
    dominant_themes: str = ""
    period_start: date
    period_end: date
    evidence_quotes: str = ""
    created_at: datetime


class BehavioralPatternBackupRecord(BackupRecord):
    id: str
    character_id: str
    kind: str
    description: str
    observed_count: int = 1
    first_observed_at: datetime
    last_observed_at: datetime
    salience: float = 0.5
