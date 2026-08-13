"""Story backup DTOs (plan §3.1「故事」).

``story_arcs`` + ``story_arc_beats`` / ``story_scene_sessions`` /
``story_events`` / ``story_seeds`` (character-private rows only) /
``character_series_progress``.
"""

from __future__ import annotations

from datetime import datetime

from kokoro_link.application.dto.character_backup.base import BackupRecord


class StoryArcBackupRecord(BackupRecord):
    id: str
    character_id: str
    title: str
    premise: str
    theme: str = "custom"
    tone: str = "daily"
    source_template_id: str | None = None
    source_seed_ids: str = "[]"
    start_date: str
    end_date: str
    status: str = "active"
    created_at: datetime
    updated_at: datetime


class StoryArcBeatBackupRecord(BackupRecord):
    id: str
    arc_id: str
    sequence: int
    scheduled_date: str
    title: str
    summary: str
    tension: str = "setup"
    status: str = "pending"
    realized_event_id: str | None = None
    play_attempt_count: int = 0
    last_play_attempt_at: datetime | None = None
    last_play_attempt_source: str | None = None
    last_play_attempt_result: str | None = None
    last_play_push_intensity: str | None = None
    play_failure_count: int = 0
    last_play_failure_at: datetime | None = None
    scene_characters: str = "[]"
    location: str | None = None
    dramatic_question: str | None = None
    scene_type: str = "encounter"
    required: bool = True
    operator_position: str | None = None
    operator_note: str | None = None


class StorySceneSessionBackupRecord(BackupRecord):
    id: str
    character_id: str
    conversation_id: str
    status: str
    source_layer: str
    arc_id: str | None = None
    beat_id: str | None = None
    title: str = ""
    location: str | None = None
    mood: str | None = None
    scene_type: str | None = None
    dramatic_question: str | None = None
    opened_at: datetime
    last_activity_at: datetime
    closed_at: datetime | None = None
    closed_reason: str | None = None
    operator_position: str | None = None
    operator_note: str | None = None


class StoryEventBackupRecord(BackupRecord):
    id: str
    character_id: str
    date: str
    seed_id: str | None = None
    arc_beat_id: str | None = None
    narrative: str
    emotional_tone: str | None = None
    memorialized: bool = False
    created_at: datetime


class StorySeedBackupRecord(BackupRecord):
    """``story_seeds`` — only rows with ``character_id IS NOT NULL``.

    The global / pack seed pool is deployment content, not character
    history; the registry carries this row filter and the export query
    (CB2) must apply it. ``character_id`` is therefore required here
    even though the column is nullable.
    """

    id: str
    seed_text: str
    tags: str = "[]"
    world_frames: str = '["any"]'
    tier: str = "daily"
    regions: str = '["global"]'
    weight: float = 1.0
    cooldown_days: int = 7
    enabled: bool = True
    language: str = "zh-TW"
    character_id: str
    external_id: str | None = None
    pack_id: str | None = None
    created_at: datetime
    updated_at: datetime


class CharacterSeriesProgressBackupRecord(BackupRecord):
    """``character_series_progress``.

    ``series_id`` references an arc series carried via the existing
    ``.lumecard`` YAML mechanism (registry: CARRY_YAML); the restore job
    must land the series first and remap ``series_id`` / ``last_arc_id``.
    """

    id: int
    character_id: str
    series_id: str
    current_index: int = 0
    status: str = "active"
    last_arc_id: str | None = None
    created_at: datetime
    updated_at: datetime
