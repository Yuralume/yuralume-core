"""``characters`` main-row backup DTO (plan §3.1 first bullet)."""

from __future__ import annotations

from datetime import date, datetime

from kokoro_link.application.dto.character_backup.base import BackupRecord


class CharacterBackupRecord(BackupRecord):
    """The character master row, minus the columns the registry excludes.

    Excluded (see ``registry.py`` for the per-column reasons):

    - B-layer deployment routing — ``voice_profile_json``,
      ``loras_json``, ``feature_models_json``,
      ``feature_image_profiles_json``, ``feature_video_profiles_json``:
      they point at providers / profiles / LoRA files the target
      instance does not have (`.lumecard` draws the same line).
    - Deployment / runtime mechanics — ``subscription_locked`` (a
      projection of the exporting deployment's Cloud tenant lock) and
      ``last_consolidated_at`` (a cross-replica claim stamp).
    - Cross-instance provenance — ``origin_official_card_id``: the
      cloud-exclusive card it names is a Cloud record, so a restore
      elsewhere would either dangle or, worse, mint a "managed"
      character with no licence behind it. A restored character is
      always an ordinary one.

    ``id`` is the *original* character id: restore reissues every id
    (D4) and uses this value only as the remap key; ``user_id`` and
    ``image_urls`` are likewise remapped / rewritten on import.
    ``frozen`` / ``frozen_at`` / ``frozen_reason`` ARE carried — a
    deliberately frozen character should come back frozen.
    """

    id: str
    user_id: str
    name: str
    summary: str = ""
    personality: str = "[]"
    interests: str = "[]"
    speaking_style: str = ""
    boundaries: str = "[]"
    aspirations: str = "[]"
    appearance: str = ""
    gender_identity: str = ""
    third_person_pronoun: str = ""
    visual_gender_presentation: str = ""
    visual_subject_type: str = "auto"
    visual_generation_style: str = ""
    date_of_birth: date | None = None
    image_urls: str = "[]"
    allowed_tools: str = "[]"
    state_emotion: str = "neutral"
    state_affection: int = 50
    state_fatigue: int = 0
    state_trust: int = 50
    state_energy: int = 100
    state_last_active_at: datetime | None = None
    state_current_intent: str | None = None
    frozen: bool = False
    frozen_at: datetime | None = None
    frozen_reason: str | None = None
    created_at: datetime
    proactive_enabled: bool = True
    proactive_daily_limit: int = 3
    proactive_cooldown_minutes: int = 30
    world_awareness_enabled: bool = False
    world_topics: str = "[]"
    subscribed_categories: str = "[]"
    excluded_topics: str = "[]"
    world_frame: str = "modern"
    accepts_web_proactive: bool = True
    unread_proactive_count: int = 0
    unread_feed_reply_count: int = 0
    image_trigger_patterns: str = "[]"
    arc_template_id: str | None = None
    arc_series_id: str | None = None
    companions_json: str = "[]"
    disposition_json: str = "{}"
    body_state_json: str = ""
    operator_pace_preference: str = ""
    personality_type_json: str = "{}"
    feed_daily_limit: int = 3
    world_id: str | None = None
