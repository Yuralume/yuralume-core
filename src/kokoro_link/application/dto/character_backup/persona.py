"""Operator-persona backup DTOs (plan §3.1「狀態與畫像」, operator half).

``operator_profile_fields`` (five-layer interpersonal portrait) /
``operator_address_preferences`` / ``operator_address_change_log`` /
``player_persona_notes`` / ``character_operator_relationship_seeds`` /
``persona_curiosity_attempts``.

``operator_id`` values are carried verbatim; the restore job remaps
every one of them to the importing operator (D4:「匯出者的
operator-scoped 資料重映射到匯入者」).
"""

from __future__ import annotations

from datetime import datetime

from kokoro_link.application.dto.character_backup.base import BackupRecord


class OperatorProfileFieldBackupRecord(BackupRecord):
    id: str
    character_id: str
    operator_id: str
    layer: int
    field_key: str
    value: str
    confidence: float
    state: str = "pending"
    source: str = "extraction"
    content_mode: str = "normal"
    evidence_json: str = "[]"
    update_count: int = 1
    explicit: bool = False
    created_at: datetime
    updated_at: datetime


class OperatorAddressPreferenceBackupRecord(BackupRecord):
    character_id: str
    operator_id: str
    salutation: str = ""
    formality_level: str = "medium"
    response_length_pref: str = "medium"
    evidence_quote: str = ""
    updated_at: datetime


class OperatorAddressChangeLogBackupRecord(BackupRecord):
    id: str
    character_id: str
    operator_id: str
    direction: str
    old_value: str = ""
    new_value: str = ""
    source: str = "player_edit"
    effective_at: datetime
    created_at: datetime


class PlayerPersonaNoteBackupRecord(BackupRecord):
    character_id: str
    operator_id: str
    note: str
    updated_at: datetime


class CharacterOperatorRelationshipSeedBackupRecord(BackupRecord):
    character_id: str
    operator_id: str
    relationship_label: str = ""
    known_context: str = ""
    living_arrangement: str = ""
    user_address_name: str = ""
    character_address_name: str = ""
    tone_distance: str = ""
    familiarity_boundary: str = ""
    schedule_involvement_policy: str = "none"
    proactive_permission: bool = False
    proactive_cadence_hint: str = ""
    user_profile_notes: str = ""
    confirmed_by_user: bool = True
    created_at: datetime
    updated_at: datetime


class PersonaCuriosityAttemptBackupRecord(BackupRecord):
    id: str
    character_id: str
    operator_id: str
    conversation_id: str | None = None
    surface: str
    target_layer: int
    target_topic: str
    question_intent: str
    status: str
    created_at: datetime
    cooldown_until: datetime | None = None
    response_turn_id: str | None = None
    metadata_json: str = "{}"
