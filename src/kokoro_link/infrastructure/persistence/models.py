"""SQLAlchemy ORM models — separate from domain entities."""

from datetime import date, datetime, timezone
from decimal import Decimal

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    BigInteger,
    CheckConstraint,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    false,
    func,
    text,
    true,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# Centralised here so the migration, ORM column, and runtime embedder
# config can't drift. If the user swaps to a different embedding model
# this is the single line to update.
MEMORY_EMBEDDING_DIM = 1024
SCHEDULE_UNKNOWN_BUSY_SCORE_DEFAULT = 0.4


class Base(DeclarativeBase):
    pass


class CharacterRow(Base):
    __tablename__ = "characters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("operator_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    """Owner anchor (per-user isolation, MULTI_USER_AUTH_PLAN Batch 1).

    Backfilled to ``"default"`` for legacy rows. Even when
    ``KOKORO_AUTH_ENABLED=false`` every character carries this so the
    schema works the same in both modes — disabling auth only bypasses
    the request-time check, never the data-model anchor."""
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    personality: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    interests: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    speaking_style: Mapped[str] = mapped_column(Text, nullable=False, default="")
    boundaries: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    aspirations: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    appearance: Mapped[str] = mapped_column(Text, nullable=False, default="")
    gender_identity: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    """Free-form character gender identity. Empty string = unset."""
    third_person_pronoun: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    """Free-form third-person pronoun for this character. Empty = unset."""
    visual_gender_presentation: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    """Free-form visual gender presentation for media prompts. Empty = unset."""
    visual_subject_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="auto", server_default="auto",
    )
    """Media body-plan hint: auto/human/animal/anthropomorphic/creature/object."""
    visual_generation_style: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", server_default="",
    )
    """Per-character generated-image style override: empty/anime/realistic."""
    date_of_birth: Mapped[date | None] = mapped_column(Date, nullable=True)
    """角色出生日期（年/月/日）。NULL = 未設定 — 與舊資料相容，
    所有衍生計算（年齡、星座、距離下一次生日的天數、是否今天生
    日）由讀取時即時計算，不快取在欄位中，避免時間流逝後資料
    與真實值不同步。"""
    image_urls: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    allowed_tools: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default='["generate_image","web_fetch","web_search"]',
    )
    """JSON-encoded list of tool names this character may invoke.

    Lives on the character row (not a join table) for the same reason
    as ``image_urls``: tiny list, rarely queried by tool name, cheaper
    to read in one go than to JOIN."""
    loras_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    """JSON-encoded list of ``{name, strength}`` pairs — LoRA weights
    applied during image generation. Kept as JSON for the same reason
    as ``allowed_tools``: small, character-scoped, co-located with the
    rest of the character row."""
    """JSON-encoded ordered list of image URLs (uploaded portraits).

    Stored as JSON text for portability and because the list is tiny;
    if this grows past single digits per character we can split to a
    dedicated table later.
    """

    # CharacterState fields (flat columns, not a separate table)
    state_emotion: Mapped[str] = mapped_column(String(100), nullable=False, default="neutral")
    state_affection: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    state_fatigue: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    state_trust: Mapped[int] = mapped_column(Integer, nullable=False, default=50)
    state_energy: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    state_last_active_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state_current_intent: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Site-level cost-control freeze (CHARACTER_FREEZE_PLAN). ``frozen``
    # halts ALL background scheduler activity for this character while
    # preserving its state; foreground chat auto-unfreezes it. Strictly
    # broader than ``proactive_enabled``.
    frozen: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false(),
    )
    frozen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    frozen_reason: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )
    """Independent character-freeze provenance: ``idle`` or ``manual``.

    ``subscription_lapse`` is a legacy value normalized by migration
    ``d2b5e8f90404``. Cloud billing state now lives in
    ``cloud_subscription_states``. See ``Character.frozen_reason``."""
    subscription_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false(),
    )
    """Retryable projection of the authoritative Cloud tenant lock."""
    last_consolidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    """Cross-replica claim stamp for post-turn memory consolidation.

    ``AutoConsolidationTrigger`` used to hold its cooldown and single-flight
    state in per-process dicts, which under 2× api replicas let both processes
    run the (LLM-heavy, memory-row-rewriting) ``consolidate()`` for the same
    character at once and halved the effective cooldown. The trigger now takes
    an atomic claim — a conditional UPDATE of this column fenced on the cooldown
    cutoff — so cooldown *and* single-flight are decided by one statement the
    DB serialises. Never written by the generic character ``save()``: like
    ``subscription_locked`` it is a dedicated control field, so an unrelated
    aggregate write can never resurrect a consumed cooldown."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    """Row creation instant — server-managed. Backfilled to the migration
    apply-time for pre-existing rows; the auto-freeze reaper uses it as
    the idle anchor for never-chatted characters."""

    proactive_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true(),
    )
    proactive_daily_limit: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    proactive_cooldown_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    world_awareness_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    world_topics: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    subscribed_categories: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]",
    )
    """JSON-encoded ``RssCategory`` allow-list. Empty = consider every
    enabled source. The curator pre-filters the candidate window by
    this before doing embedding similarity ranking."""
    excluded_topics: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]",
    )
    """JSON-encoded list of free-form topics this character avoids; the
    curator drops events whose embedding cosine to any excluded vector
    crosses an exclusion threshold."""
    world_frame: Mapped[str] = mapped_column(
        String(40), nullable=False, default="modern",
    )

    accepts_web_proactive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
    )
    """Opt-in flag for web-channel proactive delivery (see
    ``Character.accepts_web_proactive``). Existing rows default to True
    via migration so upgrades don't silently turn the feature off."""
    unread_proactive_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    """Badge counter — reset by the mark-read endpoint, incremented by
    the dispatcher's web delivery path."""
    unread_feed_reply_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    """LumeGram badge counter — incremented when a scheduler-tick
    character reply lands; zeroed by the existing ``feed/seen``
    endpoint when the user opens the overlay."""
    voice_profile_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    """JSON-encoded :class:`VoiceProfile` (per-character TTS override).
    NULL = use the global ``TTSSettings``. Serialised so future fields
    on the value object don't require schema migrations."""
    image_trigger_patterns: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    """Legacy JSON-encoded regex list.

    Kept for non-destructive compatibility with existing databases; the
    runtime now ignores it and uses the fixed ``/pic`` image command.
    """
    arc_template_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    """Hand-written arc template the next new arc should materialise
    from (Phase 2 of SCENE_BEAT_PLAN). NULL = LLM ``plan_arc`` fallback;
    a string must match a YAML template id loaded by
    ``YAMLArcTemplateRepository``. Not a foreign key — templates live
    in YAML files, not DB rows; an unknown id falls back to LLM."""
    arc_series_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True,
    )
    """Authored multi-template series the character should follow.

    NULL = legacy single-template / LLM-planned behaviour. A value
    points to ``arc_series.id`` but is intentionally not a SQL FK so
    stale bindings can fail-soft in application services instead of
    deleting character rows.
    """
    origin_official_card_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
    )
    """Provenance of an IP-partner *managed* character (EC series).

    NULL = an ordinary player-authored character; the player owns the
    persona and every API surface behaves as it always has (this is the
    only value a self-host install ever holds). Non-NULL = the row was
    installed from a cloud-exclusive official card whose id this is: the
    persona lives on the server, the player-facing projection masks it
    and the update path rejects writes to it.

    Not a foreign key — the card is a Cloud-side record, there is no
    local table to reference. Written once at install time and never by
    the aggregate ``save()`` (see ``_domain_to_row``), so no later write
    can invent or erase a character's provenance."""
    feature_models_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]",
    )
    """JSON-encoded list of ``{feature_key, provider_id, model_id}`` —
    per-character LLM routing overrides. Empty list = no overrides;
    the resolver falls through to the global ``feature_models`` pref,
    then to ``active_model``, then to the container default. Stored
    as JSON text so the column matches the rest of the small
    character-scoped lists such as ``allowed_tools`` and SQLite-backed
    unit tests don't need a JSONB shim."""
    feature_image_profiles_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]",
    )
    """JSON-encoded list of ``{feature_key, profile_id}`` — per-character
    image-profile routing overrides (mirrors ``feature_models_json`` for
    the image side). Empty list = fall through to the global picks
    (``image_feature_profiles`` per-feature pref, then
    ``active_image_profile``, then the first registered profile)."""
    feature_video_profiles_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]",
    )
    """JSON-encoded list of ``{feature_key, profile_id}`` for video-side
    routing (Wan2.2 / future video backends). Same shape as
    ``feature_image_profiles_json``; separate column so video lookups
    don't have to filter a mixed list."""
    companions_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]",
    )
    """JSON-encoded list of ``CharacterCompanion`` dicts —— per-character
    私人 NPC 同伴清單。空陣列 = 沒有同伴（預設）。Schedule planner /
    prompt builder 會讀這欄把同伴名字注入到 LLM context，讓「今天跟
    室友吃飯」這類描述能落地。"""
    disposition_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}",
    )
    """JSON-encoded :class:`CharacterDisposition` ——「內在動機傾向」
    四維 qualitative band（self_centeredness / candor / sharing_drive /
    associativeness × low / medium / high）。``"{}"`` 與全 medium 等效，
    舊資料不需要 backfill。**禁止**直接用於程式分支條件，僅供 chat /
    proactive prompt 渲染。"""
    body_state_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    """JSON-encoded :class:`BodyState` ——「具身訊號」四維 qualitative
    band（hunger / thirst / sleep_debt / seasonal_allergy × low /
    medium / high）(HUMANIZATION_ROADMAP §4.1). 空字串與全 low 等效，
    舊資料不需要 backfill。**禁止**用於程式分支條件，僅供 prompt 渲染。"""
    operator_pace_preference: Mapped[str] = mapped_column(
        String(32), nullable=False, default="", server_default="",
    )
    """Operator-facing dialogue-pace preference (HUMANIZATION_ROADMAP §3.6).

    Empty string = unset (no injection); valid values are
    ``"more_active"`` / ``"balanced"`` / ``"more_quiet"``. Per-character
    setting because each (character, operator) pair may want a
    different rhythm; the prompt builder collapses unknown values to
    no injection so a future renaming never breaks rendering."""
    personality_type_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}",
    )
    """JSON-encoded CharacterPersonalityType. Empty object / empty code =
    unset. This is a character A-layer fact for prompt/card surfaces,
    not a runtime behaviour branch."""
    feed_daily_limit: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3",
    )
    """Cap on autonomous feed posts per civil day. Mirrors
    ``proactive_daily_limit`` semantics; operator-tunable via UI."""
    world_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True,
    )
    """Opt-in link to ``worlds.id`` (revision bk0o2j80035). NULL = the
    character lives outside any world container, behaviour unchanged
    from the pre-world-system era. Not a SQL FK — character rows must
    survive world deletion; the world join service nulls this out."""


class CharacterOperatorRelationshipSeedRow(Base):
    __tablename__ = "character_operator_relationship_seeds"
    __table_args__ = (
        UniqueConstraint(
            "character_id",
            "operator_id",
            name="uq_character_operator_relationship_seed_pair",
        ),
    )

    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        primary_key=True,
    )
    operator_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("operator_profiles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    relationship_label: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    known_context: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    living_arrangement: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    user_address_name: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    character_address_name: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    tone_distance: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    familiarity_boundary: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    schedule_involvement_policy: Mapped[str] = mapped_column(
        String(32), nullable=False, default="none", server_default="none",
    )
    proactive_permission: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    proactive_cadence_hint: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    user_profile_notes: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    confirmed_by_user: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class OperatorAddressChangeLogRow(Base):
    """Audited per-pair address change (rename log). Per-(character,
    operator, direction); a global profile rename uses the alias bridge
    instead of this table."""

    __tablename__ = "operator_address_change_log"
    __table_args__ = (
        Index(
            "ix_address_change_log_pair_direction",
            "operator_id",
            "character_id",
            "direction",
            "effective_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
    )
    operator_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("operator_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    old_value: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    new_value: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="player_edit",
        server_default="player_edit",
    )
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class ConversationRow(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="web", index=True,
    )
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )
    """Optimistic-concurrency counter (B3). Every ``save()`` replace and every
    CAS ``append_messages`` bumps it under a ``FOR UPDATE`` lock so a stale
    whole-conversation ``save()`` can detect (and merge, never silently drop)
    tail rows a concurrent append added."""

    messages: Mapped[list["MessageRow"]] = relationship(
        back_populates="conversation",
        order_by="MessageRow.position",
        cascade="all, delete-orphan",
    )


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, default="chat", server_default="chat",
    )
    """Message category — ``"chat"`` for normal dialogue, ``"tool_only"``
    for bare tool-call artifacts (e.g. ``/pic`` images). Downstream
    consumers (schedule / arc / proactive) filter out ``tool_only``
    when building dialogue context."""
    attachments_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    """JSON-encoded list of ``MessageAttachment`` dicts (tool outputs).

    Empty list for user / assistant-text-only turns. Persisting on the
    message row (rather than a join table) keeps the migration trivial
    and mirrors how ``characters.image_urls`` handles tiny lists."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
        default=lambda: datetime.now(timezone.utc),
    )
    """Wall-clock time the message was authored. Indexed so the cross-
    source merge query (``recent_messages_for_character``) can sort
    messages from all of a character's conversations into one timeline
    without a sort-on-disk."""
    content_mode: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="normal",
        server_default="normal",
        index=True,
    )
    """Write-time content flow mode. ``normal`` for existing messages;
    ``nsfw`` only when the user-selected temporary mode was active."""
    safe_summary: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default="",
        server_default="",
    )
    """Frontier-safe summary for restricted message text.

    Empty means no safe replacement is available, so frontier prompt
    assembly must drop the restricted message instead of forwarding raw
    content.
    """
    idempotency_key: Mapped[str | None] = mapped_column(
        String(96), nullable=True,
    )
    """Turn-scoped idempotency key for a durable single-message append (B1).

    ``NULL`` for every ordinary append (web chat, bulk sync); set to
    ``{turn_id}:user`` / ``{turn_id}:assistant`` by the external-chat turn
    adapter. The ``uq_messages_conversation_idempotency`` unique index treats
    NULLs as distinct, so only keyed rows are de-duplicated: a takeover
    re-appending the same turn adopts the original row instead of writing a
    second copy."""

    __table_args__ = (
        UniqueConstraint(
            "conversation_id", "position",
            name="uq_messages_conversation_position",
        ),
        UniqueConstraint(
            "conversation_id", "idempotency_key",
            name="uq_messages_conversation_idempotency",
        ),
    )

    conversation: Mapped["ConversationRow"] = relationship(back_populates="messages")


class StateSnapshotRow(Base):
    """Historical record of a character state change."""

    __tablename__ = "state_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    emotion: Mapped[str] = mapped_column(String(100), nullable=False)
    affection: Mapped[int] = mapped_column(Integer, nullable=False)
    fatigue: Mapped[int] = mapped_column(Integer, nullable=False)
    trust: Mapped[int] = mapped_column(Integer, nullable=False)
    energy: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trigger: Mapped[str | None] = mapped_column(Text, nullable=True)


class CharacterGoalRow(Base):
    """Medium-term goal for a character."""

    __tablename__ = "character_goals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True, default="active")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    origin: Mapped[str] = mapped_column(String(32), nullable=False, default="manual")
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_progressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    review_notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class DailyScheduleRow(Base):
    """A character's planned day (one row per civil date)."""

    __tablename__ = "daily_schedules"
    __table_args__ = (
        # P3-Dedup (§3.4): the app-level upsert on (character_id, date) was
        # NOT DB-protected — two racing runners (a reclaimed job vs a still
        # running original) could each read-none then insert, leaving two
        # schedules for the same civil day. This constraint collapses that
        # race to exactly one row; the SA repo's save() recovers idempotently
        # from the violation instead of surfacing a 500.
        UniqueConstraint(
            "character_id", "date", name="uq_daily_schedules_character_date",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    # Civil date (YYYY-MM-DD) in the character owner's fixed user timezone.
    date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # True once the LLM planner has populated the full day; False when
    # the row was lazy-created by a chat-extracted commitment for a
    # future date (e.g. "明天 7 點看電影") and is still waiting for the
    # next ``ensure_schedule`` pass to fold it into a full day plan.
    is_planned: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true",
    )
    # Intra-day weather-drift gate marker: which activity was in progress
    # the last time the drift judge ran on this day, and the weather
    # condition phrase that was true then. NULL = never vetted. Purely a
    # cost gate (skip the LLM call while both halves are unchanged) — the
    # semantic judgement always re-reads the full schedule text.
    weather_vet_activity_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True,
    )
    weather_vet_condition: Mapped[str | None] = mapped_column(
        String(80), nullable=True,
    )

    activities: Mapped[list["ScheduleActivityRow"]] = relationship(
        back_populates="schedule",
        order_by="ScheduleActivityRow.position",
        cascade="all, delete-orphan",
    )


class ScheduleActivityRow(Base):
    """One planned activity block belonging to a ``DailyScheduleRow``."""

    __tablename__ = "schedule_activities"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("daily_schedules.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(80), nullable=False)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    busy_score: Mapped[float] = mapped_column(
        Float, nullable=False, default=SCHEDULE_UNKNOWN_BUSY_SCORE_DEFAULT,
    )
    scene_privacy: Mapped[str | None] = mapped_column(String(40), nullable=True)
    meeting_affordance: Mapped[str | None] = mapped_column(String(40), nullable=True)
    memorialized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    """Flag — this completed block was processed for idempotency."""
    has_memory: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    """Flag — the character has a persisted or matched memory for this block."""
    companion_names_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]",
    )
    """JSON-encoded list[str] — display names of companions sharing this
    activity (e.g. ``["室友小美", "同事老王"]``). Populated by the
    planner when the character has ``Character.companions`` configured;
    rendered into the chat / proactive prompt schedule block and used
    by the post-turn extractor to seed ``MemoryItem.participants`` with
    ``actor_kind="npc"`` so memories stop reading as soliloquy."""
    participant_refs_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]", server_default="[]",
    )
    """JSON-encoded list of structured ``ParticipantRef`` rows for real
    system actors in this activity. Private NPC companions remain in
    ``companion_names_json`` for backwards compatibility."""

    schedule: Mapped["DailyScheduleRow"] = relationship(back_populates="activities")


class CharacterRelationshipRow(Base):
    """Operator-approved real character pair.

    The historical migration created this table as a directed edge with
    ``from_character_id`` / ``to_character_id``. Runtime now stores the
    pair in canonical order and uses directional impression columns for
    A→B and B→A, keeping old columns populated for upgrade safety.
    """

    __tablename__ = "character_relationships"
    __table_args__ = (
        UniqueConstraint(
            "from_character_id",
            "to_character_id",
            name="uq_character_relationships_canonical_pair",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    world_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    from_character_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    to_character_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(80), nullable=False, default="", server_default="")
    affection: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    trust: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    tension: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    how_a_sees_b: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    how_b_sees_a: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    affection_a_to_b: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    affection_b_to_a: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    trust_a_to_b: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    trust_b_to_a: Mapped[int] = mapped_column(Integer, nullable=False, default=50, server_default="50")
    last_interaction_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CharacterPeerProfileRow(Base):
    """Directional stable knowledge one character has about another."""

    __tablename__ = "character_peer_profiles"
    __table_args__ = (
        UniqueConstraint(
            "character_id",
            "peer_character_id",
            name="uq_character_peer_profiles_pair",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    peer_character_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    peer_name: Mapped[str] = mapped_column(String(160), nullable=False, default="", server_default="")
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    occupation: Mapped[str] = mapped_column(String(240), nullable=False, default="", server_default="")
    haunts_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    habits_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    relationship_note: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0, server_default="0")
    last_consolidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_memory_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]", server_default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CharacterEncounterRow(Base):
    """Planned/completed real encounter between two whitelisted characters."""

    __tablename__ = "character_encounters"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    relationship_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    character_a_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    character_b_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    location: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="planned", index=True)
    trigger_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    max_turns: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    transcript_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    summary_for_a: Mapped[str] = mapped_column(Text, nullable=False, default="")
    summary_for_b: Mapped[str] = mapped_column(Text, nullable=False, default="")
    memory_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CharacterEncounterIntentRow(Base):
    """Pending chat agreement for a real character-to-character encounter."""

    __tablename__ = "character_encounter_intents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    peer_character_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    desired_after: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    topic: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="chat_agreement",
        server_default="chat_agreement",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", server_default="pending",
        index=True,
    )
    source_text: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryItemRow(Base):
    """Structured long-term memory for a character.

    Tags are serialized as a JSON-encoded string.
    """

    __tablename__ = "memory_items"
    __table_args__ = (
        Index(
            "ix_memory_items_character_created",
            "character_id",
            text("created_at DESC"),
        ),
    )
    """Keyset pagination support for the memory browser: the standalone
    ``character_id`` index still had to sort every one of a character's
    rows to answer ``ORDER BY created_at DESC LIMIT n``. Matching the
    ordering direction lets the page come straight off the index."""

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    salience: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    embedding: Mapped[list[float] | None] = mapped_column(
        Vector(MEMORY_EMBEDDING_DIM), nullable=True,
    )
    """pgvector column for semantic retrieval. Nullable so rows created
    before the embedder was configured, or while the embedder is down,
    can still be stored and later backfilled."""
    tags_embedding: Mapped[list[float] | None] = mapped_column(
        Vector(MEMORY_EMBEDDING_DIM), nullable=True,
    )
    """Auxiliary pgvector column — the embedder fed the joined tag
    string for this row (e.g. ``"travel location coffee"``). Lets
    ``query_semantic`` boost recall when the user query semantically
    matches the topic tags but not the literal content phrasing.
    Nullable for memories with no tags or rows pre-dating this column;
    the SA repo falls back to content-only similarity in that case."""
    participants_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    """JSON-encoded list of ``ParticipantRef`` dicts — see
    ``domain/value_objects/actor.py``. Phase 2 of the world-system
    roadmap: every named person in the memory content is recorded as a
    structured reference so cross-character prompts can disambiguate
    "he/she/that person" later. ``"[]"`` for memories with no
    additional participants beyond the character themselves; rows
    pre-dating Phase 2 backfill to the same default via the migration
    server_default."""
    world_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    """Reserved for the future multi-world system. Always ``None``
    today — column lands now so the world system can ship without a
    second migration + data backfill."""
    location: Mapped[str | None] = mapped_column(
        String(120), nullable=True,
    )
    """Free-form location string captured by the post-turn extractor.
    Phase 2 leaves this loose; Phase 6 (world system) normalises into
    a Place entity with FKs back to ``places``."""
    audience: Mapped[str] = mapped_column(
        String(16), nullable=False, default="", server_default="",
    )
    """Feed shareability classified by the post-turn extractor:
    ``private`` (relationship book-keeping / preferences / secrets the
    character would never broadcast), ``shareable`` (ordinary life
    moments), or ``""`` (legacy / unjudged). The LumeGram feed collector
    skips ``private`` rows; recall in chat is unaffected. ``server_default``
    keeps legacy rows feed-eligible."""


class MessagingAccountRow(Base):
    """A character's per-platform bot identity.

    Credentials are stored as JSON text (``credentials_json``) so adding
    a new platform never forces a schema change. ``webhook_slug`` is the
    unguessable URL path component used to route inbound webhooks to
    the right account; it is globally unique.

    ``UNIQUE(platform, character_id)`` keeps a character to at most one
    account per platform — good enough for the common 1:1 mapping and
    easy to relax later if somebody wants fallback bots.
    """

    __tablename__ = "messaging_accounts"
    __table_args__ = (
        UniqueConstraint(
            "platform", "character_id",
            name="uq_messaging_accounts_platform_character",
        ),
        UniqueConstraint(
            "webhook_slug", name="uq_messaging_accounts_webhook_slug",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    platform: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False, default="")
    webhook_slug: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    credentials_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    allowed_sender_refs_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    delivery_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="webhook", server_default="webhook",
    )
    polling_offset: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    polling_last_update_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    polling_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    polling_lock_owner: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    polling_lock_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ChannelBindingRow(Base):
    """A single (account, chat) pairing with its own conversation thread."""

    __tablename__ = "channel_bindings"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "chat_ref",
            name="uq_channel_bindings_account_chat",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    account_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("messaging_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chat_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    conversation_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    accepts_proactive: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )


class InboundMessageReceiptRow(Base):
    """At-most-once receipt for one inbound platform delivery.

    The cross-instance half of the inbound dedup guard (see
    ``contracts/inbound_receipts.py`` for the key-composition rationale): the
    per-process ``InboundDebouncer`` cannot stop a webhook retry that a load
    balancer lands on a second api replica, so each delivery claims a row here
    before the (synchronous, LLM-heavy) chat turn runs.

    Deliberately FK-free — the receipt is a short-lived idempotency token on
    the hot inbound path, not a relational fact; it does not need to survive
    or block account deletion and prunes out on the retention clock anyway.
    """

    __tablename__ = "inbound_message_receipts"
    __table_args__ = (
        UniqueConstraint(
            "platform", "account_id", "chat_ref", "platform_message_id",
            name="uq_inbound_message_receipts_delivery",
        ),
        # Retention sweep scans by age only.
        Index("ix_inbound_message_receipts_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    platform: Mapped[str] = mapped_column(String(32), nullable=False)
    account_id: Mapped[str] = mapped_column(String(36), nullable=False)
    chat_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    platform_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class ToolInvocationRow(Base):
    """Audit log for tool invocations (ComfyUI image gen, etc.).

    Arguments and attachment URLs are stored as JSON text for
    portability. Kept on a dedicated table instead of reusing
    ``proactive_attempts`` because the scope differs — a single
    proactive push may trigger multiple tool calls in the future.
    """

    __tablename__ = "tool_invocations"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True,
    )
    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True, default="pending",
    )
    arguments_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    output_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    attachment_urls_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class ProactiveAttemptRow(Base):
    """Audit log for proactive-messaging evaluations.

    One row per ``ProactiveDispatcher.evaluate`` call regardless of the
    outcome — including gate-blocked and decider-skipped cases — so the
    operator can introspect why the system was chatty or quiet.
    """

    __tablename__ = "proactive_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    trigger: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    binding_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("channel_bindings.id", ondelete="SET NULL"),
        nullable=True,
    )
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )


class StorySeedRow(Base):
    """Gacha-able one-line prompt for generating a story event.

    ``character_id IS NULL`` rows are global / system seeds available to
    all characters; non-null rows are private per-character seeds added
    via the UI.
    """

    __tablename__ = "story_seeds"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    seed_text: Mapped[str] = mapped_column(Text, nullable=False)
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    world_frames: Mapped[str] = mapped_column(
        Text, nullable=False, default='["any"]',
    )
    tier: Mapped[str] = mapped_column(
        String(16), nullable=False, default="daily",
    )
    """Pacing tier (``daily`` / ``dramatic``). Dramatic seeds never enter
    the daily gacha rotation — see ``StorySeed.tier``."""
    regions: Mapped[str] = mapped_column(
        Text, nullable=False, default='["global"]',
    )
    """JSON list of region codes, same JSON-in-Text convention as
    ``world_frames``; ``global`` is the wildcard."""
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    cooldown_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="zh-TW",
    )
    """Language tag of ``seed_text`` (provenance for the management UI
    badge). Bundled packs ship ``zh-TW``; never used to filter."""
    character_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    external_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True, unique=True,
    )
    """Idempotent import key for YAML packs (``core:dream:001``).
    NULL for UI-created seeds so they don't collide with pack updates."""
    pack_id: Mapped[str | None] = mapped_column(
        String(80), nullable=True, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class StoryEventRow(Base):
    """One rolled + expanded story event for a character on a day.

    Exactly one of ``seed_id`` / ``arc_beat_id`` is non-null. Gacha-
    rolled events come from a ``StorySeed`` (seed_id set); arc-driven
    events come from a ``StoryArcBeat`` (arc_beat_id set) after the
    scene actually surfaced in chat/proactive. The beat's lifecycle
    (pending → realized/skipped) is tracked on the beat row itself.
    """

    __tablename__ = "story_events"
    __table_args__ = (
        UniqueConstraint(
            "character_id", "date", "seed_id",
            name="uq_story_events_character_date_seed",
        ),
        UniqueConstraint(
            "character_id", "date", "arc_beat_id",
            name="uq_story_events_character_date_arc_beat",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    seed_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("story_seeds.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    arc_beat_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("story_arc_beats.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    emotional_tone: Mapped[str | None] = mapped_column(
        String(60), nullable=True,
    )
    memorialized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class StoryArcRow(Base):
    """Multi-week narrative spine for a character.

    At most ONE ``status = 'active'`` arc per character. This used to be a
    service-layer-only invariant guarded by a process-local ``asyncio.Lock``
    in ``StoryArcService.ensure_active_arc``; under the hosted topology
    (2× api + 2× worker) that lock is not mutual exclusion, so two replicas
    each planned a full arc (several LLM passes) and each inserted an active
    row. ``get_active_for_character`` then picked whichever had the newest
    ``updated_at``, so the narrative spine flip-flopped between two arcs.

    ``uq_story_arcs_active_character`` is the DB-level backstop: a *partial*
    unique index over ``character_id`` restricted to active rows (portable —
    both PostgreSQL and SQLite support partial indexes). Completed / abandoned
    history is unconstrained, so a character keeps as many past arcs as it
    likes. The SA repository recovers idempotently from the violation the same
    way ``daily_schedules`` does for its P3-Dedup constraint, instead of
    surfacing a 500.

    ``tone`` is the one cross-arc field that survives template
    materialisation — it carries from ArcTemplate into the runtime arc
    so the expander can switch prompts even after the template is
    edited / removed.
    """

    __tablename__ = "story_arcs"
    __table_args__ = (
        Index(
            "uq_story_arcs_active_character",
            "character_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    premise: Mapped[str] = mapped_column(Text, nullable=False)
    theme: Mapped[str] = mapped_column(String(64), nullable=False, default="custom")
    tone: Mapped[str] = mapped_column(
        String(32), nullable=False, default="daily",
    )
    source_template_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    source_seed_ids: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    """JSON list of ``story_seeds.id`` the planner folded into this arc,
    same JSON-in-Text convention as ``StorySeedRow.regions`` /
    ``StoryArcBeatRow.scene_characters``. AE2 slice — no reader/writer
    yet; a later ticket has the planner populate it."""
    start_date: Mapped[str] = mapped_column(String(10), nullable=False)
    end_date: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active", index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class StoryArcBeatRow(Base):
    """A single beat in a story arc.

    ``realized_event_id`` is not a formal FK because the story_events
    row may be deleted independently (e.g. ``delete_for_character``
    cascade), and orphaning the beat's pointer is acceptable — the
    status column is still valid and the arc UI just won't deep-link
    the event."""

    __tablename__ = "story_arc_beats"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    arc_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("story_arcs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    scheduled_date: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    tension: Mapped[str] = mapped_column(String(32), nullable=False, default="setup")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    realized_event_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    play_attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    last_play_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_play_attempt_source: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )
    last_play_attempt_result: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    last_play_push_intensity: Mapped[str | None] = mapped_column(
        String(32), nullable=True,
    )
    # --- Retry budget (SC0) -----------------------------------------
    # Split out from ``play_attempt_count`` because that column counts
    # every time the beat was surfaced — including a player's chat turns
    # — while the autonomous retry budget may only spend real failures.
    play_failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    last_play_failure_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # --- Scene structure (Phase 1 of SCENE_BEAT_PLAN) ---------------
    # JSON-encoded list of scene-character labels — keeps the "small
    # list as Text" convention (see ``allowed_tools``) so SQLite-backed
    # unit tests don't need a JSONB shim.
    scene_characters: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    dramatic_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    scene_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default="encounter",
    )
    required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
    )
    # --- Player's place in this scene (OP0) --------------------------
    # Both nullable with no server default: NULL is the domain's fourth
    # state ("unjudged"), not a missing value to be filled in, so every
    # pre-existing beat legitimately reads back as NULL and the
    # migration backfills nothing.
    operator_position: Mapped[str | None] = mapped_column(
        String(16), nullable=True,
    )
    operator_note: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppPreferenceRow(Base):
    """Simple key/value store for global UI preferences.

    Intentionally schema-less (JSON-encoded ``value``) so new UI-level
    prefs — provider/model pick, theme, language, etc. — can land
    without a migration per setting. Not a user table: Yuralume is
    single-operator, so "global" and "user" are the same scope.
    """

    __tablename__ = "app_preferences"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    """JSON-encoded value. Small strings live as ``"foo"`` rather than
    bare ``foo`` so the decoder path is uniform across shapes."""
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class WebPushSubscriptionRow(Base):
    """Browser Push API subscription bound to one operator profile."""

    __tablename__ = "web_push_subscriptions"
    __table_args__ = (
        UniqueConstraint("endpoint", name="uq_web_push_subscriptions_endpoint"),
        Index("ix_web_push_subscriptions_user_id", "user_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("operator_profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    p256dh: Mapped[str] = mapped_column(Text, nullable=False)
    auth: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    failure_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )


class NotificationPreferencesRow(Base):
    """Per-operator Web Notification delivery preferences."""

    __tablename__ = "notification_preferences"

    user_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("operator_profiles.id", ondelete="CASCADE"),
        primary_key=True,
    )
    proactive_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true(),
    )
    feed_reply_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true(),
    )
    feed_post_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false",
    )
    studio_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true(),
    )
    content_preview_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true(),
    )
    suppress_when_external_delivered: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default=true(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class CharacterAlbumItemRow(Base):
    """Long-tail image archive per character.

    Distinct from ``CharacterRow.image_urls`` (the 12-slot stage
    carousel) — this table is append-mostly and grows as tools generate
    new portraits. The row is just an index; the file lives wherever
    the original writer (ComfyUI tool, upload pipeline) put it.
    """

    __tablename__ = "character_album_items"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(
        String(16), nullable=False, default="tool",
    )
    caption: Mapped[str | None] = mapped_column(Text, nullable=True)
    byte_size: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )


class CharacterImageCandidateBatchRow(Base):
    """The one pending album-gacha candidate batch of a character.

    ``generate_candidates`` writes N throwaway objects under
    ``characters/{id}/candidates/{batch_id}/`` and ``commit_candidates``
    (a *separate* HTTP request) decides which survive. Object storage has
    no list-by-prefix call, so the keys must be remembered in between —
    and it cannot be process memory, because hosted load-balances several
    api replicas and the two requests land wherever the LB sends them.

    One row per character (``character_id`` is the primary key): a second
    generate supersedes the first, matching the UI where only the latest
    batch is on screen. The row is deleted once the batch is committed or
    discarded, so this table stays tiny.
    """

    __tablename__ = "character_image_candidate_batches"

    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        primary_key=True,
    )
    object_keys_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    """JSON-encoded ordered list[str] of candidate object keys.

    JSON text rather than a PG array for the same reason as
    ``CharacterRow.image_urls``: the list is at most
    ``MAX_CANDIDATES_PER_BATCH`` entries, is always read and written whole,
    and staying dialect-neutral keeps the SQLite migration tests runnable.
    """
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class TurnJournalRow(Base):
    """Per-turn rollback record.

    Stores a JSON payload with enough pre-turn snapshots + post-turn
    side-effect ids to reverse one chat turn. The service layer keeps at
    most 5 rows per conversation (pruned after each append); the
    foreign-key cascade to ``conversations`` cleans everything up when a
    conversation (or the owning character) is deleted.
    """

    __tablename__ = "turn_journals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    character_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True,
    )
    turn_index: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    """JSON blob holding the pre-turn snapshots (``prev_character_state``,
    ``prev_goals``, ``prev_active_arc``, ``prev_daily_schedule``,
    ``prev_active_arc_id``) and the post-turn id lists
    (``added_memory_ids``, ``added_state_snapshot_ids``,
    ``added_story_event_ids``). Kept as a single column because the
    schema of the payload is tightly coupled to
    ``turn_snapshot_codec`` and we don't query individual fields."""


class FeedPostRow(Base):
    """One feed-wall post for a character.

    Stores both the narrative payload (``content_text``,
    ``image_url``, ``image_prompt``) and a denormalised reaction
    snapshot (``likes_count`` / ``comments_count``) so the list API
    doesn't have to JOIN. Phase 2 keeps the snapshot in sync as
    reactions land; Phase 1 leaves the counters at zero.

    The ``(character_id, source_kind, source_ref_id)`` unique
    constraint provides composer-time dedup — the same beat / memory
    / activity can only seed one post, even across multiple ticks.
    """

    __tablename__ = "feed_posts"
    __table_args__ = (
        UniqueConstraint(
            "character_id",
            "source_kind",
            "source_ref_id",
            name="uq_feed_posts_character_source",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="daily",
    )
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual",
    )
    source_ref_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    image_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    video_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    """URL of a short Wan2.2 clip when the composer picked
    ``media_kind=video``. Frontend prefers ``video_url`` over
    ``image_url`` when both present."""
    video_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    """Natural-language English prompt fed to Wan2.2. Debugging /
    regenerate aid; not surfaced to end users."""
    likes_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    comments_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    reactions_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )


class FeedReactionRow(Base):
    """One like on a feed post.

    Phase A1 only models likes, so there is no ``kind`` column —
    every row is a like. The ``(post_id, liker_id)`` unique
    constraint enforces idempotency (a double-tap from the UI
    upserts to the same row instead of producing duplicates).

    Cascade is on the post side: deleting a post removes its likes
    so the count column can never drift positive after the parent
    row is gone. ``liker_id`` is free-form text — single-user mode
    stamps everything as ``LOCAL_LIKER_ID`` today, but messaging
    bots / multi-user later can drop their own identity in without
    a schema change.
    """

    __tablename__ = "feed_reactions"
    __table_args__ = (
        UniqueConstraint(
            "post_id", "liker_id",
            name="uq_feed_reactions_post_liker",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    post_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("feed_posts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    liker_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )


class FeedCommentRow(Base):
    """One user comment on a feed post.

    Phase A2 only models user → character comments — no replies, no
    character-authored comments. Comments are not idempotent: every
    submission creates a new row. ``author_id`` is free-form text
    (mirrors ``liker_id`` on FeedReactionRow) so multi-user / bot
    comments slot in later without a migration.

    Cascade on the post side keeps comment counts honest after a post
    is deleted.
    """

    __tablename__ = "feed_comments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    post_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("feed_posts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
    )
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )


class OperatorProfileRow(Base):
    """The human operator behind the chat window.

    Phase 1 of the world-system roadmap. Today this is effectively
    singleton (``DEFAULT_OPERATOR_ID = "default"``), but the table
    is keyed by id so the eventual multi-operator world doesn't need
    a migration — only a different resolver picking which row is
    "current".

    ``aliases_json`` is a JSON-encoded list of strings (alternate
    names the extractor should treat as the operator). Stored as
    plain text rather than a JSON column to stay portable — the rest
    of the schema does this too.
    """

    __tablename__ = "operator_profiles"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    aliases_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    display_name_locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    """True once the player explicitly edits their display name, so a
    cloud OAuth re-login won't clobber it (see cloud_auth_service)."""
    pronouns: Mapped[str | None] = mapped_column(String(64), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """Login identifier when ``KOKORO_AUTH_ENABLED=true``. NULL on the
    pre-setup default user — front-end uses this to detect "needs
    setup" and route to /setup. Partial-unique index enforces no
    duplicate emails but allows multiple NULL rows."""
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    """bcrypt/argon2 hash. NULL on default user before first setup
    completes; AuthService refuses to authenticate users with NULL
    password_hash so a half-migrated install can't be brute-forced."""
    is_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )
    """Owner / admin flag — controls access to user CRUD endpoints.
    Migration ct5y7z00070 marks the existing ``"default"`` row admin
    on upgrade so the single-user install keeps full control after
    enabling multi-user mode."""
    primary_language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="zh-TW", server_default="zh-TW",
    )
    """BCP 47 tag pinned at setup/register. Drives the LLM **content**
    language (chat, memory, persona, story, feed). Immutable after
    creation — see ``OperatorProfile.update`` docstring and migration
    cu6z8a10071. The frontend UI locale switcher is independent."""
    timezone_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="UTC", server_default="UTC",
    )
    """IANA timezone id for user-facing civil dates and visible clock
    times. DB/server instants stay UTC; this is not character-specific."""
    country_code: Mapped[str | None] = mapped_column(String(2), nullable=True)
    """ISO 3166-1 alpha-2 country code for location-aware fact providers."""
    latitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    longitude: Mapped[float | None] = mapped_column(Float, nullable=True)
    location_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """Human-readable location label for prompt fact display."""
    cloud_account_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, unique=True,
    )
    """Yuralume Cloud account id for hosted-core federated projections."""
    cloud_tenant_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    """Yuralume Cloud tenant id associated with the projected account."""
    cloud_tenant_tier: Mapped[str] = mapped_column(
        String(32), nullable=False, default="standard", server_default="standard",
    )
    """Yuralume Cloud tenant tier, used by hosted runtime policy resolution."""
    auth_provider: Mapped[str] = mapped_column(
        String(16), nullable=False, default="local", server_default="local",
    )
    """Identity source: local self-host auth or Yuralume Cloud federation."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class CloudSubscriptionStateRow(Base):
    """Authoritative desired subscription lock for one Cloud tenant."""

    __tablename__ = "cloud_subscription_states"

    tenant_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    locked: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class PendingFollowUpRow(Base):
    """Deferred reply queued because the character was too busy.

    Lifecycle is driven by ``PendingFollowUpDispatcher`` running on the
    proactive scheduler's tick: rows with ``status='queued'`` and
    ``scheduled_for <= now`` whose character has dropped below the busy
    threshold get released into a full reply, fanned out through the
    same path proactive messages use.

    ``messages_json`` carries the FIFO-merged user messages — the queue
    keeps growing if the user types again while waiting (merge-don't-
    cancel policy, hard cap of ``MAX_QUEUED_MESSAGES`` enforced at the
    service layer). ``brief_reply`` is the in-character ack the user
    already saw inline so the deferred LLM can honour that promise.

    FK cascade to ``conversations`` keeps the table clean when a
    conversation is deleted; a separate cascade by ``character_id``
    runs in ``CharacterService.delete_character`` because the column
    is not a foreign key (TG/LINE conversations belonging to bindings
    already cascade through their own path).
    """

    __tablename__ = "pending_follow_ups"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36), nullable=False, index=True,
    )
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", index=True,
    )
    activity_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    brief_reply: Mapped[str] = mapped_column(Text, nullable=False)
    defer_reason: Mapped[str] = mapped_column(
        Text, nullable=False, default="",
    )
    messages_json: Mapped[str] = mapped_column(Text, nullable=False)
    """JSON array of ``{content, queued_at(ISO), message_id?}`` rows.
    FIFO; new arrivals append. Service layer caps length at
    ``MAX_QUEUED_MESSAGES``."""
    scheduled_for: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    resolved_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # ``kind`` discriminates "busy_defer" (legacy) from "scheduled_promise"
    # (user explicitly asked the character to message them at this time).
    # Stored as string so new kinds don't need a migration; default keeps
    # every legacy row routed through the busy-defer composer.
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="busy_defer",
        server_default="busy_defer",
    )
    promise_intent: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    """Natural-language description of what the character promised to do
    at ``scheduled_for``. Empty string for ``kind=busy_defer`` rows."""


class OperatorProfileFieldRow(Base):
    """One observed fact about the operator, **per character**, layered
    by the five-tier interpersonal model.

    Per-character (not operator-global) so a brand-new character starts
    from zero observations — matches real social: when you meet someone
    new, they don't inherit what your old friends know about you. The
    "stranger → acquaintance" arc each character walks is the whole
    point of the feature; sharing across characters would collapse it.

    Staging and confirmed rows share this table — the ``state`` column
    distinguishes them. The unique key carries ``character_id`` so the
    same field_key can have an independent confirmed + pending shadow
    set under every character the operator talks to.

    ``evidence_json`` is a JSON-encoded list of
    ``{turn_id, conversation_id, quote, extracted_at}`` rows
    (:meth:`EvidenceRef.to_dict`). Stored inline rather than as a
    separate evidence table because evidence is only ever fetched along
    with its parent field — normalising would multiply round trips
    without gaining query power.
    """

    __tablename__ = "operator_profile_fields"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    operator_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("operator_profiles.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    layer: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    field_key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="pending", index=True,
    )
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="extraction")
    content_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="normal", server_default="normal",
    )
    evidence_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    update_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    explicit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "character_id", "operator_id", "layer", "field_key", "state",
            "value",
            name="uq_operator_profile_fields_per_character_state",
        ),
    )


class EmotionEventRow(Base):
    """Append-only emotion event log behind ``EmotionAggregator``.

    Schema mirrors :class:`EmotionEvent`. ``cause_ref_kind`` +
    ``cause_ref_id`` is the polymorphic-FK pair the same way
    ``feed_posts.source_kind_text`` / ``source_ref_id`` works.
    """

    __tablename__ = "emotion_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operator_id: Mapped[str] = mapped_column(String(64), nullable=False)
    cause_ref_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    cause_ref_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    valence: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    arousal: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    intensity: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)
    affection_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fatigue_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    trust_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    energy_delta: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    applied_to_state: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
    )
    emotion_label: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_quote: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decay_half_life_minutes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=240,
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )


class DispositionDriftHistoryRow(Base):
    """Audit row for one ``CharacterDisposition`` band shift
    (HUMANIZATION_ROADMAP §3.1).

    Powers the 人格演化軌跡 admin timeline and enforces the per-
    dimension 30-day cooldown.
    """

    __tablename__ = "disposition_drift_history"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dimension: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    from_band: Mapped[str] = mapped_column(String(8), nullable=False)
    to_band: Mapped[str] = mapped_column(String(8), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    evidence_quote: Mapped[str] = mapped_column(Text, nullable=False, default="")
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )


class SelfReflectionRow(Base):
    """Persisted ``SelfReflection`` row (HUMANIZATION_ROADMAP §3.2).

    At most one current row per ``(character_id, operator_id, period)``.
    Re-running the dream-time generator overwrites the prior snapshot;
    older versions are not preserved (out-of-scope archival).
    """

    __tablename__ = "self_reflections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operator_id: Mapped[str] = mapped_column(String(64), nullable=False)
    period: Mapped[str] = mapped_column(String(16), nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    dominant_themes: Mapped[str] = mapped_column(
        Text, nullable=False, default="",
    )
    """JSON-encoded ``list[str]`` of theme tags."""
    period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)
    evidence_quotes: Mapped[str] = mapped_column(
        Text, nullable=False, default="",
    )
    """JSON-encoded ``list[str]`` of verbatim citations."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )

    __table_args__ = (
        UniqueConstraint(
            "character_id", "operator_id", "period",
            name="uq_self_reflections_per_pair_period",
        ),
    )


class BehavioralPatternRow(Base):
    """Persisted ``BehavioralPattern`` row (HUMANIZATION_ROADMAP §3.3).

    Unique on ``(character_id, kind, description)`` so the dream pass
    can upsert idempotently — repeated detections bump
    ``observed_count`` + ``last_observed_at`` instead of bloating the
    table with duplicates.
    """

    __tablename__ = "behavioral_patterns"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    observed_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1,
    )
    first_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    last_observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    salience: Mapped[float] = mapped_column(Float, nullable=False, default=0.5)

    __table_args__ = (
        UniqueConstraint(
            "character_id", "kind", "description",
            name="uq_behavioral_patterns_per_character_kind_desc",
        ),
    )


class DeferredIntentRow(Base):
    """Persisted ``DeferredIntent`` row (HUMANIZATION_ROADMAP §3.4).

    Stores motives the proactive ``intention_judge`` blocked, with a
    TTL. Re-surfaced as a fact-layer prompt block on subsequent judge
    calls so the LLM can re-evaluate timing instead of forgetting the
    motive the moment one bad tick passes.
    """

    __tablename__ = "deferred_intents"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operator_id: Mapped[str] = mapped_column(String(64), nullable=False)
    trigger: Mapped[str] = mapped_column(String(48), nullable=False, default="tick")
    inner_motive: Mapped[str] = mapped_column(Text, nullable=False, default="")
    conversation_purpose: Mapped[str] = mapped_column(Text, nullable=False, default="")
    expected_reply: Mapped[str] = mapped_column(Text, nullable=False, default="")
    risk: Mapped[str] = mapped_column(Text, nullable=False, default="")
    best_timing: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="active", index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    revisit_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    """When the judge named a concrete future moment for this motive
    ("we agreed on 19:30"). NULL for every motive without a clock —
    which is every row written before this column existed."""


class ExperimentRow(Base):
    """A/B experiment row (HUMANIZATION_ROADMAP §4.6)."""

    __tablename__ = "experiments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    variants_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    salt: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    active: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class ExperimentAssignmentRow(Base):
    """Sticky variant assignment (HUMANIZATION_ROADMAP §4.6)."""

    __tablename__ = "experiment_assignments"

    experiment_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("experiments.id", ondelete="CASCADE"),
        primary_key=True,
    )
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        primary_key=True,
    )
    operator_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    variant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class OperatorAddressPreferenceRow(Base):
    """Observed operator address / register preference (HUMANIZATION_ROADMAP §4.2).

    Composite PK (character_id, operator_id) — one row per pair. Owner
    decision (2026-05-21): the observation overrides the §3.6 explicit
    pace preference; the priority rule lives in the prompt builder.
    """

    __tablename__ = "operator_address_preferences"

    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        primary_key=True,
    )
    operator_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    salutation: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    formality_level: Mapped[str] = mapped_column(
        String(16), nullable=False, default="medium",
    )
    response_length_pref: Mapped[str] = mapped_column(
        String(16), nullable=False, default="medium",
    )
    evidence_quote: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class PlayerPersonaNoteRow(Base):
    """Player-declared identity / world premise for one pair.

    Composite PK (character_id, operator_id) — one declaration per pair.
    Deliberately separate from ``operator_profile_fields``: that table is
    what the character inferred (confidence, evidence, decay), this one is
    what the player asserted, which none of those columns apply to.
    """

    __tablename__ = "player_persona_notes"

    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        primary_key=True,
    )
    operator_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    note: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class PersonaCuriosityAttemptRow(Base):
    """Audit ledger for conversational persona discovery.

    Rows record the character's attempt to learn about the operator.
    They are process facts used by the LLM curiosity planner, not
    confirmed operator-persona facts.
    """

    __tablename__ = "persona_curiosity_attempts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operator_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    conversation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    surface: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    target_layer: Mapped[int] = mapped_column(Integer, nullable=False)
    target_topic: Mapped[str] = mapped_column(String(80), nullable=False)
    question_intent: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    cooldown_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    response_turn_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class AppRuntimeSettingRow(Base):
    """Global per-installation runtime-mutable KV (HUMANIZATION_ROADMAP §4.5).

    Generic key/value table whose first user is quiet-hours window
    persistence. Reads fall back to env defaults when a key is missing,
    so an empty table behaves identically to the legacy env-only setup.
    """

    __tablename__ = "app_runtime_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class ProviderConnectionRow(Base):
    """Encrypted BYOK provider connection.

    Secrets are encrypted by the application before hitting the DB.
    ``config_json`` intentionally stores only non-sensitive settings
    such as base URL, default model, region, or default voice.
    """

    __tablename__ = "provider_connections"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    capabilities_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    config_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    encrypted_secret_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="",
    )
    secret_fingerprint: Mapped[str] = mapped_column(
        String(64), nullable=False, default="",
    )
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    last_validation_error: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True,
    )


class TurnRecordRow(Base):
    """Per-LLM-turn audit row for replay / evals / observability.

    One row per turn regardless of outcome — including proactive
    evaluations the gate blocked before any LLM call. Distinct from
    ``TurnJournalRow`` (which is a pre-turn rollback snapshot).
    """

    __tablename__ = "turn_records"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True,
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    prompt_pack_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", index=True,
    )
    prompt_assembled: Mapped[str] = mapped_column(Text, nullable=False, default="")
    response_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    response_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    """JSON-encoded parsed structured output for turns that emit JSON
    (post-turn processor, intention judge, decider, dream). ``NULL`` for
    free-form chat turns."""
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    post_turn_refs: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    """JSON-encoded dict of refs to side effects produced by this turn
    (memory_ids, state_change_id, proactive_attempt_id, ...). Schema is
    open by design — each turn ``kind`` carries its own shape."""
    operator_feedback: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    """JSON-encoded owner/operator feedback used by eval fixture mining."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )


class AccountRuntimeEventRow(Base):
    """Account-level runtime policy ledger.

    These rows are business-limit events, not provider metering. They
    intentionally survive deletion of resources like TTL-reaped characters so
    rolling-window quotas cannot be reset by deleting and recreating.
    """

    __tablename__ = "account_runtime_events"
    __table_args__ = (
        Index(
            "ix_account_runtime_events_operator_type_time",
            "operator_id",
            "event_type",
            "occurred_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operator_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("operator_profiles.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    resource_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True,
    )


class StudioGenerationJobRow(Base):
    """Durable Creator Studio pipeline job (fusion / branching).

    ``target_id`` is deliberately not a foreign key — it may point at a
    ``fusion_stories`` or ``branching_dramas`` row, and a deleted target
    must degrade to a failed job instead of blocking the delete.
    """

    __tablename__ = "studio_generation_jobs"
    __table_args__ = (
        Index(
            "ix_studio_generation_jobs_status_updated",
            "status",
            "updated_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(48), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True,
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1,
    )
    params_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}",
    )
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )


class PendingFeedVideoRow(Base):
    """A composed feed post waiting on an asynchronous video job (CV4).

    The row *is* the restart story for the deferred pipeline: the tick
    that submitted the job publishes nothing, so everything the landing
    half needs — the draft, the already-rendered first frame, the job
    ticket and its deadline — lives here until one of the two exits fires.

    ``character_id`` cascades: a deleted character can have no post to
    land, and leaving the row would strand a poll chain on a ghost. The
    ``(status, next_poll_at)`` index is the sweep's only query.
    """

    __tablename__ = "pending_feed_videos"
    __table_args__ = (
        Index(
            "ix_pending_feed_videos_status_next_poll",
            "status",
            "next_poll_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    operator_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="",
    )
    job_id: Mapped[str] = mapped_column(
        String(128), nullable=False, index=True,
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, index=True,
    )
    content_text: Mapped[str] = mapped_column(Text, nullable=False)
    post_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="daily",
    )
    source_kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default="manual",
    )
    source_ref_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    first_frame_url: Mapped[str] = mapped_column(Text, nullable=False)
    first_frame_key: Mapped[str] = mapped_column(
        Text, nullable=False, default="",
    )
    image_prompt: Mapped[str] = mapped_column(
        Text, nullable=False, default="",
    )
    video_prompt: Mapped[str] = mapped_column(
        Text, nullable=False, default="",
    )
    submitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    timeout_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=900,
    )
    next_poll_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    post_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    note: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class GenerationUsageEventRow(Base):
    """Provider/resource usage row for LLM, image, video, and TTS calls."""

    __tablename__ = "generation_usage_events"
    __table_args__ = (
        Index("ix_generation_usage_character_created", "character_id", "created_at"),
        Index("ix_generation_usage_capability_created", "capability", "created_at"),
        Index("ix_generation_usage_feature_created", "feature_key", "created_at"),
        Index(
            "ix_generation_usage_provider_model_created",
            "provider_id",
            "model_id",
            "created_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(
        String(128), nullable=False, default="", index=True,
    )
    upstream_request_id: Mapped[str] = mapped_column(
        String(128), nullable=False, default="",
    )
    turn_record_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, index=True,
    )
    conversation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    character_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    operator_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    capability: Mapped[str] = mapped_column(String(32), nullable=False)
    feature_key: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    source_surface: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    routing_mode: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    provider_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    model_id: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    profile_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    voice_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    prompt_pack_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    usage_unit: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    input_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    billable_quantity: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    prompt_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cached: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    usage_is_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
    )
    cost_currency: Mapped[str] = mapped_column(String(8), nullable=False, default="USD")
    cost_amount: Mapped[Decimal] = mapped_column(
        Numeric(18, 8), nullable=False, default=0,
    )
    cost_is_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
    )
    pricing_source: Mapped[str] = mapped_column(
        String(64), nullable=False, default="unknown",
    )
    pricing_version: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    latency_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="succeeded",
    )
    error_code: Mapped[str | None] = mapped_column(String(128), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_seconds: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 3), nullable=True,
    )
    content_hash: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    metadata_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class MemoirPinRow(Base):
    """Player-side memoir pin row.

    Side table over ``MemoryItem`` / ``EmotionEvent`` / ``SelfReflection``
    that records which timeline entries a given ``(character_id,
    operator_id)`` pair has pinned. Pins are display-layer ordering hints
    only — pinning never edits the underlying memory row. The unique
    constraint enforces per-pair isolation and idempotent re-pin.
    """

    __tablename__ = "memoir_pins"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
    )
    operator_id: Mapped[str] = mapped_column(String(64), nullable=False)
    entry_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    """One of ``memory`` / ``emotion`` / ``milestone`` — see
    :mod:`kokoro_link.domain.entities.memoir` for the canonical set."""
    entry_id: Mapped[str] = mapped_column(String(64), nullable=False)
    """Source row id (``MemoryItem.id`` or ``EmotionEvent.id``)."""
    pinned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )

    __table_args__ = (
        UniqueConstraint(
            "character_id", "operator_id", "entry_kind", "entry_id",
            name="uq_memoir_pins_per_pair_entry",
        ),
    )


class ArcTemplateRow(Base):
    """Story-arc template row (pack-shipped or user-authored).

    The ``id`` column doubles as the slug ``Character.arc_template_id``
    references — pack slugs and user-authored slugs share one namespace
    so character → template binding stays a single primary-key fetch.
    Pack rows carry ``user_id IS NULL`` and are visible to every user;
    user-authored rows carry an ``operator_profiles.id`` and are gated
    by the ownership guard in the repository layer.
    """

    __tablename__ = "arc_templates"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("operator_profiles.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    pack_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True,
    )
    """Source YAML filename stem on pack rows; ``None`` on user rows.

    Used by ``ArcTemplatePackSyncService`` to match DB rows back to
    disk files during the startup upsert. Not foreign-keyed because
    YAML files aren't a DB resource."""
    external_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True,
    )
    """Original ``id`` field declared inside the YAML, kept for
    provenance when the file stem and the declared id disagree."""
    title: Mapped[str] = mapped_column(Text, nullable=False)
    premise: Mapped[str] = mapped_column(Text, nullable=False)
    theme: Mapped[str] = mapped_column(
        String(64), nullable=False, default="custom",
    )
    tone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="daily",
    )
    language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="zh-TW",
    )
    """Authored-prose language tag (metadata for badge + translate
    decision). Pack rows ship ``zh-TW``; never used to filter."""
    duration_days: Mapped[int] = mapped_column(
        Integer, nullable=False, default=14,
    )
    world_frames_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    required_traits_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    applicability_scope: Mapped[str] = mapped_column(
        String(32), nullable=False, default="generic",
    )
    target_character_ids_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    beats_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class ArcSeriesRow(Base):
    """Authored multi-template story series (pack-shipped or user-owned)."""

    __tablename__ = "arc_series"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("operator_profiles.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    pack_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True,
    )
    external_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    premise: Mapped[str] = mapped_column(Text, nullable=False)
    theme: Mapped[str] = mapped_column(
        String(64), nullable=False, default="custom",
    )
    tone: Mapped[str] = mapped_column(
        String(64), nullable=False, default="dramatic",
    )
    world_frames_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    required_traits_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="[]",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class ArcSeriesMemberRow(Base):
    """Ordered ArcTemplate member inside an ArcSeries."""

    __tablename__ = "arc_series_members"
    __table_args__ = (
        UniqueConstraint(
            "series_id",
            "template_id",
            name="uq_arc_series_members_series_template",
        ),
        UniqueConstraint(
            "series_id",
            "position",
            name="uq_arc_series_members_series_position",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    series_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("arc_series.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    template_id: Mapped[str] = mapped_column(String(64), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)


class CharacterSeriesProgressRow(Base):
    """Per-character progress through a bound ArcSeries."""

    __tablename__ = "character_series_progress"
    __table_args__ = (
        UniqueConstraint(
            "character_id",
            "series_id",
            name="uq_character_series_progress_character_series",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    series_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("arc_series.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    current_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="active",
    )
    last_arc_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


# ---------------------------------------------------------------------------
# Durable background-job queue foundation (P2-A / HOSTED_CORE_SCALING §3).
#
# These five tables are ADDITIVE and DORMANT: no running process reads or
# writes them yet. They exist so the distributed Hosted topology
# (coordinator / worker / realtime outbox) can land in P2-B/P4 without a
# second schema migration + backfill. Self-host stays single-container and
# never touches them.
# ---------------------------------------------------------------------------


class BackgroundJobRow(Base):
    """One durable background job (queue row).

    ``tenant_id`` / ``operator_id`` / ``character_id`` are intentionally
    NOT foreign keys — a job must degrade to ``dead``/``failed`` if its
    target vanished, never block a delete or orphan-cascade. ``payload_json``
    holds IDs / dates / config versions ONLY (never prompts, tokens or chat
    content); ``last_error`` holds an exception class + sanitised summary
    (never a secret). See ``contracts/background_jobs.py``.
    """

    __tablename__ = "background_jobs"
    __table_args__ = (
        # Ready-queue scan: cheapest possible for the hot claim path.
        Index(
            "ix_background_jobs_ready",
            "status",
            "due_at",
            "priority",
            postgresql_where=text("status = 'queued'"),
        ),
        # Fairness / diagnostics per tenant.
        Index(
            "ix_background_jobs_tenant_status",
            "tenant_id",
            "status",
        ),
        # At most one ACTIVE (queued|claimed) row per logical key; the same
        # key may recur once the previous occurrence reached a terminal
        # status. This is the DB-enforced idempotency guarantee.
        Index(
            "uq_background_jobs_active_idem",
            "idempotency_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'claimed')"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operator_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    character_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    due_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    priority: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="queued", server_default="queued",
    )
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5, server_default="5",
    )
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    fencing_epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1",
    )
    payload_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}",
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class BackgroundRuntimeLeaseRow(Base):
    """TTL leader lease with a monotonic fencing epoch.

    ``epoch`` is bumped ``+1`` on every ownership change and never reset,
    so a partitioned ex-leader's stale epoch can be rejected on write.
    """

    __tablename__ = "background_runtime_leases"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default="",
    )
    epoch: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0",
    )
    lease_until: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class BackgroundCoordinatorCursorRow(Base):
    """Durable due-discovery bookmark for the coordinator (§4.2).

    ``cursor_json`` holds IDs / timestamps only.
    """

    __tablename__ = "background_coordinator_cursors"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class SchedulerTickJournalRow(Base):
    """Append-only scheduler-tick journal for embedded/distributed parity.

    Written by P2-B's shadow-gated embedded journal; schema only here.
    ``kind`` is ``character_tick`` (with ``character_id``) or ``social_tick``
    (``character_id`` NULL — the cross-character global bucket).
    """

    __tablename__ = "scheduler_tick_journal"
    __table_args__ = (
        Index(
            "ix_scheduler_tick_journal_bucket_kind",
            "tick_bucket",
            "kind",
        ),
        Index(
            "uq_scheduler_tick_journal_character",
            "tick_bucket",
            "kind",
            "character_id",
            unique=True,
            postgresql_where=text("character_id IS NOT NULL"),
            sqlite_where=text("character_id IS NOT NULL"),
        ),
        Index(
            "uq_scheduler_tick_journal_social",
            "tick_bucket",
            "kind",
            unique=True,
            postgresql_where=text("character_id IS NULL"),
            sqlite_where=text("character_id IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    tick_bucket: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    character_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class RealtimeEventRow(Base):
    """Transactional realtime-event outbox row.

    DORMANT until Phase 4 (§7.1): laid down now per §13 Phase 2's
    "jobs/leases/events/cursors additive migrations" so the outbox can
    ship without a second migration. Nothing reads or writes this table
    in P2-A/P2-B; the SSE relay (§7.0) is the interim bridge.
    """

    __tablename__ = "realtime_events"
    __table_args__ = (
        # Composite ``(created_at, id)`` supports both the Phase 4 outbox reads:
        # the ``read_after`` overlap-window range scan on ``created_at`` (with
        # ``id`` covered for the ascending order + de-dup) and the ``prune``
        # ``created_at < cutoff`` range. Supersedes the original single-column
        # ``ix_realtime_events_created`` (leftmost prefix keeps prune covered).
        Index("ix_realtime_events_created_id", "created_at", "id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    tenant_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    operator_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(
        Text, nullable=False, default="{}", server_default="{}",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class BackgroundVisibleSlotRow(Base):
    """At-most-once claim ledger for visible per-character background output.

    P3-Dedup (§3.4/§6.2): under real distributed execution a reclaimed job
    can race a still-running original worker on the SAME character. The
    per-source feed/story/reflection surfaces are already DB-protected by
    their own UNIQUE constraints; the proactive-send and feed-comment-reply
    passes were not. This table is the additive claim ledger those passes
    take: the first executor to INSERT ``(character_id, kind, slot)`` owns the
    slot for that tick bucket; a losing executor's INSERT trips the unique
    index and it skips silently. ``slot`` is the tick-bucket id the caller
    computes (string). Rows prune on the 7-day clock via ``created_at``.
    """

    __tablename__ = "background_visible_slots"
    __table_args__ = (
        UniqueConstraint(
            "character_id", "kind", "slot",
            name="uq_background_visible_slots_claim",
        ),
        Index("ix_background_visible_slots_created", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    slot: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class BackgroundExecutionModeRow(Base):
    """Single-row execution-ownership flag (HOSTED_CORE_SCALING §2.2 / §13
    Phase 3).

    The database source of truth that makes embedded / distributed background
    execution MUTUALLY EXCLUSIVE (§15). Exactly one logical row keyed
    ``name='default'``; ``epoch`` is a monotonic CAS token bumped +1 on every
    flip and never reset. An ABSENT row means ``('embedded', 0)`` — the
    self-host red line, so no seed row ships. Deliberately NOT the TTL-CAS
    ``background_runtime_leases`` table: a mode flag must not be stealable by
    ordinary lease-acquire traffic once a TTL lapses.
    """

    __tablename__ = "background_execution_mode"

    name: Mapped[str] = mapped_column(String(32), primary_key=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    epoch: Mapped[int] = mapped_column(BigInteger, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )


class ExternalChatTurnReceiptRow(Base):
    """Durable receipt for one recoverable external-chat turn (LH2, DR-LH0-004).

    The spine of the ``source="line"`` turn state machine
    (``PROCESSING → GENERATED → COMMITTED → COMPLETED`` /
    ``FAILED_RETRYABLE`` / ``FAILED_TERMINAL``). ``UNIQUE(authenticated_caller,
    request_id)`` enforces at-most-one receipt per cross-service idempotency id;
    ``owner_id`` + ``lease_version`` fence every mutation so a stale (taken-over)
    owner cannot write. ``tenant_id`` / ``account_id`` / ``character_id`` are
    NOT foreign keys — a receipt must survive its target changing. Snapshot
    columns hold opaque JSON owned by the turn service. See
    ``contracts/external_chat_turn.py``.
    """

    __tablename__ = "external_chat_turn_receipts"
    __table_args__ = (
        UniqueConstraint(
            "authenticated_caller",
            "request_id",
            name="uq_external_chat_turn_receipts_caller_request",
        ),
        # Recovery scan: expired-lease takeover candidates by state.
        Index(
            "ix_external_chat_turn_receipts_state_lease",
            "state",
            "lease_until",
        ),
        Index(
            "ix_external_chat_turn_receipts_conversation",
            "conversation_id",
        ),
        CheckConstraint(
            "channel = 'line'",
            name="ck_external_chat_turn_receipts_channel",
        ),
    )

    turn_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    authenticated_caller: Mapped[str] = mapped_column(
        String(64), nullable=False,
    )
    request_id: Mapped[str] = mapped_column(String(128), nullable=False)
    contract_version: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True,
    )
    canonical_hash_version: Mapped[int | None] = mapped_column(
        SmallInteger, nullable=True,
    )
    canonical_request_hash: Mapped[str] = mapped_column(
        String(64), nullable=False,
    )
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    character_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    state: Mapped[str] = mapped_column(String(24), nullable=False)
    owner_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    lease_version: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=1, server_default="1",
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    conversation_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True,
    )
    user_message_row_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    assistant_message_row_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    assistant_position: Mapped[int | None] = mapped_column(
        Integer, nullable=True,
    )
    generated_snapshot_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    response_snapshot_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    terminal_error_json: Mapped[str | None] = mapped_column(
        Text, nullable=True,
    )
    post_turn_state: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending",
    )
    post_turn_job_key: Mapped[str | None] = mapped_column(
        String(160), nullable=True,
    )
    last_error_code: Mapped[str | None] = mapped_column(
        String(64), nullable=True,
    )
    last_error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    committed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )


class ExternalProactiveEventRow(Base):
    """Durable pre-send ledger for one external proactive delivery event.

    DR-LH0-005: Core records the immutable ``event_id + envelope + payload_hash``
    BEFORE calling the channel, so a lost response re-sends the SAME event
    without re-running the proactive judge / decider. ``tenant_id`` /
    ``account_id`` / ``character_id`` are NOT foreign keys — the event must
    survive its target changing. ``envelope_json`` / ``payload_hash`` are
    write-once; only ``state`` / ``accept_attempts`` / ``last_error`` /
    ``updated_at`` change. See ``contracts/external_proactive_ledger.py``.
    """

    __tablename__ = "external_proactive_events"
    __table_args__ = (
        # Pending-retry / expiry sweep scan by state then validity horizon.
        Index(
            "ix_external_proactive_events_state_expires",
            "state",
            "expires_at",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(64), nullable=False)
    account_id: Mapped[str] = mapped_column(String(64), nullable=False)
    character_id: Mapped[str] = mapped_column(String(64), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    envelope_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(16), nullable=False)
    accept_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation_recorded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false(),
    )
    """Whether the delivered proactive turn has been appended to the
    character's ``source="line"`` conversation history (M8). ``accepted`` marks
    the transport handoff; this marks the durable history write. An accepted
    row still ``False`` here is the retry worker's worklist for a lost /
    crashed history append, so the channel-accepted send and the conversation
    row share one lifecycle instead of the append being fire-and-forget."""
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )



class StorySceneSessionRow(Base):
    """One player-pulled story scene («起幕») and its live state.

    The scene's messages live in the ordinary conversation; this row is
    the *frame* around them plus everything a later request needs to keep
    playing it — which waterfall layer supplied the material, which beat
    is being played, the visible title/location/mood, the dramatic
    question, and the idle clock. It has to be a row rather than a
    process attribute because the replica that opens a scene is routinely
    not the one that serves its next turn or closes it on timeout.

    ``uq_story_scene_sessions_open_character`` — a partial unique index
    over ``character_id`` restricted to ``status = 'open'`` — makes "one
    live scene per character" a schema invariant, the same portable shape
    ``story_arcs`` uses for its single-active-arc rule. Closed rows are
    unconstrained so the played-scene history accumulates.

    ``arc_id`` / ``beat_id`` carry no FK on purpose: the arc repository
    replaces a whole arc's beat rows on every save, so ``ON DELETE SET
    NULL`` would unlink a live scene from its beat during an unrelated
    arc edit (same reasoning as ``realized_event_id``).
    """

    __tablename__ = "story_scene_sessions"
    __table_args__ = (
        Index(
            "uq_story_scene_sessions_open_character",
            "character_id",
            unique=True,
            postgresql_where=text("status = 'open'"),
            sqlite_where=text("status = 'open'"),
        ),
        Index(
            "ix_story_scene_sessions_status_activity",
            "status",
            "last_activity_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    character_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("characters.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    conversation_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    source_layer: Mapped[str] = mapped_column(String(24), nullable=False)
    arc_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    beat_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    title: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default="",
    )
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    mood: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scene_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    dramatic_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    last_activity_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    closed_reason: Mapped[str | None] = mapped_column(
        String(24), nullable=True,
    )
    # --- Player's place in this scene (OP2-D) -------------------------
    # Copied from the opening StorySceneMaterial, same reasoning as
    # scene_type / dramatic_question above: the closer reads only this
    # row, never the beat.
    operator_position: Mapped[str | None] = mapped_column(
        String(length=16), nullable=True,
    )
    operator_note: Mapped[str | None] = mapped_column(Text, nullable=True)
