"""DTO round-trip tests for the ``.lumebackup`` per-table records (CB0).

Representative coverage (the registry pin test guarantees *breadth* —
every carried table's DTO matches its schema; here we prove the value
conventions hold): ORM row → ``from_row`` → JSON (the jsonl boundary) →
parse → ``to_row_kwargs`` → ORM row again, with identical column values.
Plus the old-version tolerance the plan leans on (pydantic defaults) and
the schema-version constant.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

from kokoro_link.application.dto.character_backup import (
    BACKUP_SCHEMA_VERSION,
    CharacterBackupManifest,
    CharacterBackupRecord,
    DailyScheduleBackupRecord,
    MemoryItemBackupRecord,
    MessageBackupRecord,
    ScheduleActivityBackupRecord,
    SelfReflectionBackupRecord,
    StoryArcBeatBackupRecord,
    StorySeedBackupRecord,
)
from kokoro_link.infrastructure.persistence.models import (
    CharacterRow,
    DailyScheduleRow,
    MemoryItemRow,
    MessageRow,
    ScheduleActivityRow,
    SelfReflectionRow,
    StoryArcBeatRow,
    StorySeedRow,
)


_NOW = datetime(2026, 8, 5, 12, 30, 45, tzinfo=timezone.utc)


def _roundtrip(record_cls, row):  # noqa: ANN001, ANN202
    """DTO → JSON → DTO → row kwargs; returns the rebuilt kwargs."""
    record = record_cls.from_row(row)
    payload = record.model_dump_json()
    parsed = record_cls.model_validate_json(payload)
    assert parsed == record
    return parsed.to_row_kwargs()


def test_backup_schema_version_is_one() -> None:
    assert BACKUP_SCHEMA_VERSION == 1
    assert CharacterBackupManifest(
        character_id="c1",
    ).schema_version == 1


def test_character_record_round_trip_excludes_b_layer() -> None:
    row = CharacterRow(
        id="char-1",
        user_id="default",
        name="芊璃",
        summary="測試角色",
        personality='["gentle"]',
        interests='["tea"]',
        speaking_style="soft",
        boundaries="[]",
        aspirations="[]",
        appearance="銀髮",
        gender_identity="女性",
        third_person_pronoun="她",
        visual_gender_presentation="feminine",
        visual_subject_type="human",
        visual_generation_style="anime",
        date_of_birth=date(2003, 3, 14),
        image_urls='["https://obj/characters/char-1/0.png"]',
        allowed_tools='["generate_image"]',
        loras_json='[{"name": "style", "strength": 0.8}]',
        state_emotion="happy",
        state_affection=77,
        state_fatigue=5,
        state_trust=66,
        state_energy=90,
        state_last_active_at=_NOW,
        state_current_intent="泡茶",
        frozen=True,
        frozen_at=_NOW,
        frozen_reason="manual",
        subscription_locked=True,
        last_consolidated_at=_NOW,
        created_at=_NOW,
        proactive_enabled=True,
        proactive_daily_limit=5,
        proactive_cooldown_minutes=45,
        world_awareness_enabled=True,
        world_topics='["astronomy"]',
        subscribed_categories='["news"]',
        excluded_topics='["politics"]',
        world_frame="modern",
        accepts_web_proactive=True,
        unread_proactive_count=2,
        unread_feed_reply_count=1,
        voice_profile_json='{"voice_id": "v1"}',
        image_trigger_patterns="[]",
        arc_template_id="tpl-1",
        arc_series_id="series-1",
        feature_models_json='[{"feature_key": "chat"}]',
        feature_image_profiles_json="[]",
        feature_video_profiles_json="[]",
        companions_json='[{"name": "室友小美"}]',
        disposition_json='{"candor": "high"}',
        body_state_json="",
        operator_pace_preference="balanced",
        personality_type_json='{"code": "INFP"}',
        feed_daily_limit=4,
        world_id=None,
    )

    record = CharacterBackupRecord.from_row(row)
    # B-layer / deployment columns are not even fields on the DTO.
    for absent in (
        "voice_profile_json",
        "loras_json",
        "feature_models_json",
        "feature_image_profiles_json",
        "feature_video_profiles_json",
        "subscription_locked",
        "last_consolidated_at",
    ):
        assert absent not in type(record).model_fields, absent

    kwargs = _roundtrip(CharacterBackupRecord, row)
    rebuilt = CharacterRow(**kwargs)
    assert rebuilt.id == "char-1"
    assert rebuilt.name == "芊璃"
    assert rebuilt.date_of_birth == date(2003, 3, 14)
    assert rebuilt.state_last_active_at == _NOW
    assert rebuilt.frozen is True
    assert rebuilt.companions_json == '[{"name": "室友小美"}]'


def test_message_record_round_trip_keeps_mode_and_summary() -> None:
    row = MessageRow(
        id=42,
        conversation_id="conv-1",
        position=7,
        role="assistant",
        content="raw text",
        kind="chat",
        attachments_json='[{"url": "https://obj/x.png"}]',
        created_at=_NOW,
        content_mode="nsfw",
        safe_summary="a safe recap",
        idempotency_key="turn-9:assistant",
    )
    kwargs = _roundtrip(MessageBackupRecord, row)
    rebuilt = MessageRow(**kwargs)
    assert rebuilt.id == 42
    assert rebuilt.position == 7
    assert rebuilt.content_mode == "nsfw"
    assert rebuilt.safe_summary == "a safe recap"
    assert rebuilt.created_at == _NOW


def test_memory_item_record_has_no_vector_fields() -> None:
    row = MemoryItemRow(
        id="mem-1",
        character_id="char-1",
        conversation_id=None,
        kind="episodic",
        content="今天一起看了流星雨",
        salience=0.9,
        tags='["stargazing"]',
        created_at=_NOW,
        last_accessed_at=None,
        access_count=3,
        embedding=[0.5] * 4,
        tags_embedding=None,
        participants_json="[]",
        world_id=None,
        location="山丘",
        audience="shareable",
    )
    record = MemoryItemBackupRecord.from_row(row)
    assert "embedding" not in type(record).model_fields
    assert "tags_embedding" not in type(record).model_fields

    kwargs = _roundtrip(MemoryItemBackupRecord, row)
    assert "embedding" not in kwargs
    rebuilt = MemoryItemRow(**kwargs)
    assert rebuilt.content == "今天一起看了流星雨"
    assert rebuilt.salience == 0.9


def test_schedule_records_round_trip() -> None:
    schedule = DailyScheduleRow(
        id="sched-1",
        character_id="char-1",
        date="2026-08-05",
        generated_at=_NOW,
        is_planned=True,
        weather_vet_activity_id=None,
        weather_vet_condition=None,
    )
    activity = ScheduleActivityRow(
        id="act-1",
        schedule_id="sched-1",
        position=0,
        start_at=_NOW,
        end_at=_NOW,
        description="晨跑",
        category="exercise",
        location="河堤",
        busy_score=0.7,
        scene_privacy=None,
        meeting_affordance=None,
        memorialized=False,
        has_memory=True,
        companion_names_json='["室友小美"]',
        participant_refs_json="[]",
    )
    schedule_kwargs = _roundtrip(DailyScheduleBackupRecord, schedule)
    assert DailyScheduleRow(**schedule_kwargs).date == "2026-08-05"

    activity_kwargs = _roundtrip(ScheduleActivityBackupRecord, activity)
    rebuilt = ScheduleActivityRow(**activity_kwargs)
    assert rebuilt.start_at == _NOW
    assert rebuilt.companion_names_json == '["室友小美"]'


def test_story_beat_and_seed_round_trip() -> None:
    beat = StoryArcBeatRow(
        id="beat-1",
        arc_id="arc-1",
        sequence=2,
        scheduled_date="2026-08-09",
        title="轉折",
        summary="她收到了那封信",
        tension="rising",
        status="pending",
        realized_event_id=None,
        play_attempt_count=1,
        last_play_attempt_at=_NOW,
        last_play_attempt_source="chat",
        last_play_attempt_result="deferred",
        last_play_push_intensity="soft",
        play_failure_count=0,
        last_play_failure_at=None,
        scene_characters='["旧友"]',
        location="車站",
        dramatic_question="她會回信嗎",
        scene_type="encounter",
        required=True,
        operator_position="participant",
        operator_note=None,
    )
    beat_kwargs = _roundtrip(StoryArcBeatBackupRecord, beat)
    assert StoryArcBeatRow(**beat_kwargs).operator_position == "participant"

    seed = StorySeedRow(
        id="seed-1",
        seed_text="在舊書店遇到常客",
        tags='["daily"]',
        world_frames='["modern"]',
        tier="daily",
        regions='["global"]',
        weight=1.5,
        cooldown_days=7,
        enabled=True,
        language="zh-TW",
        character_id="char-1",
        external_id=None,
        pack_id=None,
        created_at=_NOW,
        updated_at=_NOW,
    )
    seed_kwargs = _roundtrip(StorySeedBackupRecord, seed)
    assert StorySeedRow(**seed_kwargs).character_id == "char-1"


def test_self_reflection_dates_survive_json() -> None:
    row = SelfReflectionRow(
        id="refl-1",
        character_id="char-1",
        operator_id="default",
        period="weekly",
        narrative="這週更常想起你",
        dominant_themes='["longing"]',
        period_start=date(2026, 7, 27),
        period_end=date(2026, 8, 2),
        evidence_quotes='["…"]',
        created_at=_NOW,
    )
    kwargs = _roundtrip(SelfReflectionBackupRecord, row)
    rebuilt = SelfReflectionRow(**kwargs)
    assert rebuilt.period_start == date(2026, 7, 27)
    assert rebuilt.period_end == date(2026, 8, 2)


def test_old_archive_missing_fields_falls_back_to_defaults() -> None:
    """An archive written before a defaulted field existed still loads —
    the plan's version tolerance is pydantic defaults, so a v1 reader
    build must accept a payload without them."""
    parsed = MessageBackupRecord.model_validate_json(
        '{"id": 1, "conversation_id": "conv-1", "position": 0,'
        ' "role": "user", "content": "hi",'
        ' "created_at": "2026-08-05T12:30:45Z"}',
    )
    assert parsed.kind == "chat"
    assert parsed.content_mode == "normal"
    assert parsed.safe_summary == ""
    assert parsed.attachments_json == "[]"


def test_unknown_extra_fields_are_ignored() -> None:
    parsed = MessageBackupRecord.model_validate_json(
        '{"id": 1, "conversation_id": "conv-1", "position": 0,'
        ' "role": "user", "content": "hi",'
        ' "created_at": "2026-08-05T12:30:45Z",'
        ' "field_from_a_minor_future": true}',
    )
    assert parsed.content == "hi"


def test_naive_datetimes_from_sqlite_normalise_to_utc() -> None:
    row = MessageRow(
        id=1,
        conversation_id="conv-1",
        position=0,
        role="user",
        content="hi",
        kind="chat",
        attachments_json="[]",
        created_at=datetime(2026, 8, 5, 12, 30, 45),  # naive
        content_mode="normal",
        safe_summary="",
        idempotency_key=None,
    )
    record = MessageBackupRecord.from_row(row)
    assert record.created_at.tzinfo is not None
    assert record.created_at == _NOW
