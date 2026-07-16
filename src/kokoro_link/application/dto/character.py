from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from kokoro_link.contracts.image_profile import FeatureImageProfileOverride  # noqa: F401  (used by TYPE_CHECKING / from_domain)
from kokoro_link.contracts.video_profile import FeatureVideoProfileOverride  # noqa: F401
from kokoro_link.domain.entities.character import (
    DEFAULT_ALLOWED_TOOLS,
    Character,
    CharacterLora,
    FeatureModelOverride,
)
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
    SCHEDULE_INVOLVEMENT_POLICIES,
)
from kokoro_link.domain.entities.state_snapshot import StateSnapshot
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.companion import CharacterCompanion
from kokoro_link.domain.value_objects.disposition import CharacterDisposition
from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)
from kokoro_link.domain.value_objects.voice_profile import VoiceProfile
from kokoro_link.domain.value_objects.visual_subject import (
    DEFAULT_VISUAL_SUBJECT_TYPE,
    VisualSubjectType,
    normalise_visual_subject_type,
)
from kokoro_link.domain.value_objects.visual_generation_style import (
    normalise_character_visual_generation_style,
)


ProactiveRhythm = Literal["quiet", "balanced", "lively"]

_PROACTIVE_RHYTHM_PRESETS: dict[ProactiveRhythm, tuple[int, int]] = {
    "quiet": (1, 180),
    "balanced": (3, 30),
    "lively": (6, 15),
}


def proactive_rhythm_values(rhythm: ProactiveRhythm) -> tuple[int, int]:
    return _PROACTIVE_RHYTHM_PRESETS[rhythm]


def proactive_rhythm_from_values(
    *,
    daily_limit: int,
    cooldown_minutes: int,
) -> ProactiveRhythm:
    if daily_limit <= 1 or cooldown_minutes >= 120:
        return "quiet"
    if daily_limit >= 5 or cooldown_minutes <= 20:
        return "lively"
    return "balanced"


class ProactiveRhythmRequest(BaseModel):
    """Player-facing proactive cadence preset.

    The player chooses a qualitative rhythm; the backend owns the
    mapping to numeric daily-limit and cooldown fields so the player UI
    never has to expose raw scheduling thresholds.
    """

    rhythm: ProactiveRhythm


class CharacterCompanionPayload(BaseModel):
    """One private NPC companion entry.

    Sent both ways: client → server in create/update payloads, server →
    client in :class:`CharacterResponse`. ``id`` is optional on inbound
    payloads — when missing we mint a fresh UUID so the operator's UI
    doesn't need to generate ids for net-new companions. Existing ids
    survive round-trips so memories pointing at this companion via
    ``ParticipantRef.actor_id`` stay linked across edits.

    All strings are stripped + length-capped by the domain layer
    (:class:`CharacterCompanion.__post_init__`), so the DTO can stay
    permissive about whitespace and overlong input."""

    id: str | None = None
    name: str
    role: str = ""
    brief_profile: str = ""
    personality_sketch: list[str] = Field(default_factory=list)
    relationship_snippet: str = ""

    @classmethod
    def from_domain(
        cls, companion: CharacterCompanion,
    ) -> "CharacterCompanionPayload":
        return cls(
            id=companion.id,
            name=companion.name,
            role=companion.role,
            brief_profile=companion.brief_profile,
            personality_sketch=list(companion.personality_sketch),
            relationship_snippet=companion.relationship_snippet,
        )

    def to_domain(self) -> CharacterCompanion | None:
        """Construct the value object, returning ``None`` when the
        payload would otherwise raise (e.g. blank ``name``)."""
        try:
            return CharacterCompanion.create(
                id_=self.id or None,
                name=self.name,
                role=self.role,
                brief_profile=self.brief_profile,
                personality_sketch=tuple(self.personality_sketch),
                relationship_snippet=self.relationship_snippet,
            )
        except ValueError:
            return None


class CharacterLoraPayload(BaseModel):
    name: str
    strength: float = 1.0

    @classmethod
    def from_domain(cls, lora: CharacterLora) -> "CharacterLoraPayload":
        return cls(name=lora.name, strength=lora.strength)


class CharacterPersonalityTypePayload(BaseModel):
    """16 型性格 payload。

    ``code=""`` 表示未設定。非空未知 code 由 domain value object fail loud，
    讓 create/update API 不會靜默落錯 typo。
    """

    system: str = "mbti_16"
    code: str = ""
    source: str = "unset"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    rationale: str = ""
    consistency_notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_domain_value(self) -> "CharacterPersonalityTypePayload":
        try:
            self.to_domain()
        except ValueError as exc:
            raise ValueError(str(exc)) from exc
        return self

    @classmethod
    def from_domain(
        cls, personality_type: CharacterPersonalityType,
    ) -> "CharacterPersonalityTypePayload":
        return cls(**personality_type.to_payload())

    def to_domain(self) -> CharacterPersonalityType:
        return CharacterPersonalityType(
            system=self.system,
            code=self.code,
            source=self.source,
            confidence=self.confidence,
            rationale=self.rationale,
            consistency_notes=tuple(self.consistency_notes),
        )


class FeatureModelOverridePayload(BaseModel):
    """One per-character LLM override row.

    Same blank-means-fallback semantics as the global feature_models
    pref: blank ``provider_id`` falls through to the global picker;
    blank ``model_id`` lets the resolved provider pick its own default.
    Sending an entry with both fields blank is equivalent to omitting
    the row — the service drops it before persisting."""

    feature_key: str
    provider_id: str | None = None
    model_id: str | None = None

    @classmethod
    def from_domain(
        cls, entry: FeatureModelOverride,
    ) -> "FeatureModelOverridePayload":
        return cls(
            feature_key=entry.feature_key,
            provider_id=entry.provider_id,
            model_id=entry.model_id,
        )

    def to_domain(self) -> FeatureModelOverride | None:
        """Return ``None`` for blank-and-blank entries so the service
        layer can filter them out without re-checking the rule."""
        try:
            override = FeatureModelOverride(
                feature_key=self.feature_key,
                provider_id=self.provider_id,
                model_id=self.model_id,
            )
        except ValueError:
            return None
        return None if override.is_empty else override


class FeatureVideoProfileOverridePayload(BaseModel):
    """One per-character video-profile override row.

    Mirror of :class:`FeatureImageProfileOverridePayload`. Blank
    ``profile_id`` falls through to the global picks (per-feature →
    active → first registered)."""

    feature_key: str
    profile_id: str | None = None

    @classmethod
    def from_domain(
        cls, entry: "FeatureVideoProfileOverride",
    ) -> "FeatureVideoProfileOverridePayload":
        return cls(
            feature_key=entry.feature_key,
            profile_id=entry.profile_id,
        )

    def to_domain(self) -> "FeatureVideoProfileOverride | None":
        from kokoro_link.contracts.video_profile import (
            FeatureVideoProfileOverride,
        )

        try:
            override = FeatureVideoProfileOverride(
                feature_key=self.feature_key,
                profile_id=self.profile_id,
            )
        except ValueError:
            return None
        return None if override.is_empty else override


class FeatureImageProfileOverridePayload(BaseModel):
    """One per-character image-profile override row.

    Mirrors :class:`FeatureModelOverridePayload` — blank ``profile_id``
    falls through to the global picks. Sending an entry with a null
    ``profile_id`` is equivalent to omitting the row (the service drops
    it before persisting)."""

    feature_key: str
    profile_id: str | None = None

    @classmethod
    def from_domain(
        cls, entry: "FeatureImageProfileOverride",
    ) -> "FeatureImageProfileOverridePayload":
        return cls(
            feature_key=entry.feature_key,
            profile_id=entry.profile_id,
        )

    def to_domain(self) -> "FeatureImageProfileOverride | None":
        from kokoro_link.contracts.image_profile import (
            FeatureImageProfileOverride,
        )

        try:
            override = FeatureImageProfileOverride(
                feature_key=self.feature_key,
                profile_id=self.profile_id,
            )
        except ValueError:
            return None
        return None if override.is_empty else override


class CharacterDispositionPayload(BaseModel):
    """API payload for :class:`CharacterDisposition` (內在動機傾向).

    四維 qualitative band。``""`` / 缺欄 / 未知值都會在 domain 層被
    normalise 成 ``"medium"``；非 ``"low" / "medium" / "high"`` 的非空字串
    會丟 :class:`ValueError` 由 service 轉成 422。
    """

    self_centeredness: str = "medium"
    candor: str = "medium"
    sharing_drive: str = "medium"
    associativeness: str = "medium"

    @classmethod
    def from_domain(
        cls, disposition: CharacterDisposition,
    ) -> "CharacterDispositionPayload":
        return cls(
            self_centeredness=disposition.self_centeredness,
            candor=disposition.candor,
            sharing_drive=disposition.sharing_drive,
            associativeness=disposition.associativeness,
        )

    def to_domain(self) -> CharacterDisposition:
        return CharacterDisposition(
            self_centeredness=self.self_centeredness,
            candor=self.candor,
            sharing_drive=self.sharing_drive,
            associativeness=self.associativeness,
        )


class BodyStatePayload(BaseModel):
    """API payload for :class:`BodyState` (具身訊號，§4.1).

    四維 qualitative band, default all ``"low"`` = 沒任何身體不適。Owner
    decision (2026-05-21): 月經週期相位本批不做，不在 schema 中。"""

    hunger: str = "low"
    thirst: str = "low"
    sleep_debt: str = "low"
    seasonal_allergy: str = "low"

    @classmethod
    def from_domain(cls, state) -> "BodyStatePayload":  # noqa: ANN001
        return cls(
            hunger=state.hunger,
            thirst=state.thirst,
            sleep_debt=state.sleep_debt,
            seasonal_allergy=state.seasonal_allergy,
        )

    def to_domain(self):  # noqa: ANN201
        from kokoro_link.domain.value_objects.body_state import BodyState
        return BodyState(
            hunger=self.hunger,
            thirst=self.thirst,
            sleep_debt=self.sleep_debt,
            seasonal_allergy=self.seasonal_allergy,
        )


class VoiceProfilePayload(BaseModel):
    """Per-character TTS override.

    Empty-string fields fall back to the global :class:`TTSSettings`
    at synth time, so a partial profile (e.g. only the prompt overridden)
    still works. Sending all fields blank with ``enabled=true`` is
    equivalent to omitting the profile entirely — the service drops
    it before persisting."""

    enabled: bool = True
    voice_id: str = ""
    ref_audio_path: str = ""
    prompt_text: str = ""
    prompt_lang: str = ""
    translate_target_lang: str = ""
    gpt_weights_path: str = ""
    sovits_weights_path: str = ""

    @classmethod
    def from_domain(cls, profile: VoiceProfile) -> "VoiceProfilePayload":
        return cls(
            enabled=profile.enabled,
            voice_id=profile.voice_id,
            ref_audio_path=profile.ref_audio_path,
            prompt_text=profile.prompt_text,
            prompt_lang=profile.prompt_lang,
            translate_target_lang=profile.translate_target_lang,
            gpt_weights_path=profile.gpt_weights_path,
            sovits_weights_path=profile.sovits_weights_path,
        )

    def to_domain(self) -> VoiceProfile | None:
        return VoiceProfile.from_payload(self.model_dump())


class CharacterStatePayload(BaseModel):
    emotion: str = Field(default="neutral")
    affection: int = Field(default=0, ge=0, le=100)
    fatigue: int = Field(default=0, ge=0, le=100)
    trust: int = Field(default=0, ge=0, le=100)
    energy: int = Field(default=100, ge=0, le=100)
    last_active_at: datetime | None = None
    current_intent: str | None = None


class InitialRelationshipSafeUserProfilePayload(BaseModel):
    name: str = ""
    nickname: str = ""
    occupation: str = ""
    company_or_school: str = ""
    interests: list[str] = Field(default_factory=list)
    routine: str = ""
    life_goals: list[str] = Field(default_factory=list)

    def has_values(self) -> bool:
        return any((
            self.name.strip(),
            self.nickname.strip(),
            self.occupation.strip(),
            self.company_or_school.strip(),
            any(item.strip() for item in self.interests),
            self.routine.strip(),
            any(item.strip() for item in self.life_goals),
        ))


class InitialRelationshipPayload(BaseModel):
    relationship_label: str = ""
    known_context: str = ""
    living_arrangement: str = Field(default="", max_length=240)
    user_address_name: str = ""
    character_address_name: str = ""
    tone_distance: str = ""
    familiarity_boundary: str = ""
    schedule_involvement_policy: str = "none"
    proactive_permission: bool = False
    proactive_cadence_hint: str = ""
    user_profile_notes: str = ""
    confirmed_by_user: bool = True
    safe_user_profile: InitialRelationshipSafeUserProfilePayload = Field(
        default_factory=InitialRelationshipSafeUserProfilePayload,
    )

    @model_validator(mode="after")
    def _validate_schedule_policy(self) -> "InitialRelationshipPayload":
        policy = (self.schedule_involvement_policy or "none").strip().lower()
        if policy not in SCHEDULE_INVOLVEMENT_POLICIES:
            raise ValueError(
                "InitialRelationship.schedule_involvement_policy must be one of "
                f"{sorted(SCHEDULE_INVOLVEMENT_POLICIES)}, got "
                f"{self.schedule_involvement_policy!r}",
            )
        self.schedule_involvement_policy = policy
        return self

    def to_seed(
        self,
        *,
        character_id: str,
        operator_id: str,
        now: datetime,
    ) -> CharacterOperatorRelationshipSeed:
        return CharacterOperatorRelationshipSeed(
            character_id=character_id,
            operator_id=operator_id,
            relationship_label=self.relationship_label,
            known_context=self.known_context,
            living_arrangement=self.living_arrangement,
            user_address_name=self.user_address_name,
            character_address_name=self.character_address_name,
            tone_distance=self.tone_distance,
            familiarity_boundary=self.familiarity_boundary,
            schedule_involvement_policy=self.schedule_involvement_policy,
            proactive_permission=self.proactive_permission,
            proactive_cadence_hint=self.proactive_cadence_hint,
            user_profile_notes=self.user_profile_notes,
            confirmed_by_user=self.confirmed_by_user,
            created_at=now,
            updated_at=now,
        )


class CreateCharacterRequest(BaseModel):
    name: str
    summary: str = ""
    personality: list[str] = Field(default_factory=list)
    interests: list[str] = Field(default_factory=list)
    speaking_style: str = "natural"
    boundaries: list[str] = Field(default_factory=list)
    aspirations: list[str] = Field(default_factory=list)
    appearance: str = ""
    gender_identity: str = ""
    """Free-form character gender identity / self-description.

    Empty string means unknown / unset. The backend stores this as a
    character fact and does not infer it from name, summary, or
    appearance.
    """
    third_person_pronoun: str = ""
    """Free-form third-person pronoun for this character.

    Empty string means unknown / unset. Player-facing UI should fall
    back to the character name or neutral wording instead of assuming a
    gendered pronoun.
    """
    visual_gender_presentation: str = ""
    """Free-form visual gender presentation used by media prompts.

    Kept separate from grammatical pronoun so users can describe visual
    identity without forcing a single gender enum.
    """
    visual_subject_type: VisualSubjectType = DEFAULT_VISUAL_SUBJECT_TYPE
    """Media body-plan hint. One of auto/human/animal/anthropomorphic/
    creature/object. ``auto`` keeps legacy behaviour unless Core can
    safely infer a non-human animal from appearance text."""
    visual_generation_style: str = ""
    """Per-character image style override. ``""`` inherits user/global
    visual-generation-style; explicit values are ``"anime"`` or
    ``"realistic"``. Creation UI may set this before the automatic first
    portrait is generated."""
    date_of_birth: date | None = None
    """角色出生日期（ISO `YYYY-MM-DD`）。``null`` 表示未設定，後端
    所有生日衍生提示（年齡、星座、距離下一次生日的天數）都會略過。"""
    image_urls: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(
        default_factory=lambda: list(DEFAULT_ALLOWED_TOOLS),
    )
    loras: list[CharacterLoraPayload] = Field(default_factory=list)
    initial_state: CharacterStatePayload = Field(default_factory=CharacterStatePayload)
    proactive_enabled: bool = True
    proactive_daily_limit: int = Field(default=3, ge=0, le=50)
    proactive_cooldown_minutes: int = Field(default=30, ge=1, le=24 * 60)
    feed_daily_limit: int = Field(default=3, ge=0, le=50)
    """Daily cap on autonomous feed-wall posts. ``0`` disables the
    feed for this character entirely (the composer's ``_is_feed_enabled``
    short-circuits before any LLM / image work)."""
    world_awareness_enabled: bool = False
    world_topics: list[str] = Field(default_factory=list)
    subscribed_categories: list[str] = Field(default_factory=list)
    """RSS category allow-list for this character's event-inbox curator."""
    excluded_topics: list[str] = Field(default_factory=list)
    """Free-form topics to drop from the inbox (embedding-based filter)."""
    world_frame: str = "modern"
    accepts_web_proactive: bool = True
    arc_template_id: str | None = None
    """Bundled arc-template id (Phase 2 of SCENE_BEAT_PLAN). ``None`` =
    LLM ``plan_arc`` fallback (legacy behaviour)."""
    arc_series_id: str | None = None
    """Optional authored arc-series id. When set, runtime advances through
    the series members instead of asking the planner to invent the next
    season."""
    feature_models: list[FeatureModelOverridePayload] = Field(default_factory=list)
    """Per-character LLM routing overrides. Empty list = no overrides
    (the global ``feature_models`` / ``active_model`` prefs apply)."""
    feature_image_profiles: list[FeatureImageProfileOverridePayload] = Field(
        default_factory=list,
    )
    """Per-character image-profile routing overrides. Mirrors
    ``feature_models`` for the image side."""
    feature_video_profiles: list[FeatureVideoProfileOverridePayload] = Field(
        default_factory=list,
    )
    """Per-character video-profile routing overrides."""
    companions: list[CharacterCompanionPayload] = Field(default_factory=list)
    """私人 NPC 同伴清單（同事、室友、家人…）。空 list = 沒有同伴
    (預設)。建立角色時可一次給齊；之後也能透過 PATCH 更新或從
    ``POST /characters/{id}/companions/generate`` 取得 AI 建議。"""
    disposition: CharacterDispositionPayload = Field(
        default_factory=CharacterDispositionPayload,
    )
    """內在動機傾向四維（self_centeredness / candor / sharing_drive /
    associativeness × low / medium / high）。全省略或全 medium = 等同
    「沒設定」，prompt 不會渲染。"""
    body_state: BodyStatePayload = Field(default_factory=BodyStatePayload)
    """HUMANIZATION_ROADMAP §4.1 — 具身訊號四維（hunger / thirst /
    sleep_debt / seasonal_allergy × low / medium / high）。全 low = 沒
    身體不適，prompt 跳過渲染。owner decision (2026-05-21): 月經週期相位本批不做。"""
    operator_pace_preference: str = ""
    """HUMANIZATION_ROADMAP §3.6 — 使用者對「這個角色」希望的對話節奏。
    合法值：``""`` (預設未設定) / ``"more_active"`` / ``"balanced"`` /
    ``"more_quiet"``。未知值會被後端 normalise 成 ``""``。"""
    personality_type: CharacterPersonalityTypePayload = Field(
        default_factory=CharacterPersonalityTypePayload,
    )
    """角色的 16 型性格創作參考。未設定時 code 為空字串；非空未知 code
    會由 domain 層拒絕。"""
    initial_relationship: InitialRelationshipPayload | None = None
    """創角時由使用者確認的 per-character/operator 起始關係 context。
    不寫入 Character.summary；角色卡 manifest 不會攜帶它，但匯入者可在
    confirm import request 中明確提供本地起始關係。"""


class UpdateCharacterRequest(BaseModel):
    name: str | None = None
    summary: str | None = None
    personality: list[str] | None = None
    interests: list[str] | None = None
    speaking_style: str | None = None
    boundaries: list[str] | None = None
    aspirations: list[str] | None = None
    appearance: str | None = None
    gender_identity: str | None = None
    third_person_pronoun: str | None = None
    visual_gender_presentation: str | None = None
    visual_subject_type: VisualSubjectType | None = None
    visual_generation_style: str | None = None
    # Tri-state — see ``arc_template_id`` below. Omit = leave alone;
    # ``null`` = clear back to unknown; ``"YYYY-MM-DD"`` = set the date.
    date_of_birth: date | None = Field(default=None)
    image_urls: list[str] | None = None
    allowed_tools: list[str] | None = None
    loras: list[CharacterLoraPayload] | None = None
    state: CharacterStatePayload | None = None
    proactive_enabled: bool | None = None
    proactive_daily_limit: int | None = Field(default=None, ge=0, le=50)
    proactive_cooldown_minutes: int | None = Field(default=None, ge=1, le=24 * 60)
    feed_daily_limit: int | None = Field(default=None, ge=0, le=50)
    world_awareness_enabled: bool | None = None
    world_topics: list[str] | None = None
    subscribed_categories: list[str] | None = None
    excluded_topics: list[str] | None = None
    world_frame: str | None = None
    accepts_web_proactive: bool | None = None
    # Tri-state semantics: omit field = leave alone; ``null`` = unbind
    # template (back to LLM-only); string id = bind to that template.
    # Pydantic v2 represents "missing" via the model field metadata;
    # downstream readers use ``model_fields_set`` to distinguish null
    # from omitted. The character_service layer translates the JSON
    # null → ``None`` → domain unbind.
    arc_template_id: str | None = Field(default=None)
    # Same tri-state semantics as ``arc_template_id``. Omit = leave the
    # current series binding alone; null = unbind; string id = bind.
    arc_series_id: str | None = Field(default=None)
    feature_models: list[FeatureModelOverridePayload] | None = None
    """Per-character LLM overrides. ``None`` (omitted) leaves existing
    overrides untouched; ``[]`` clears them; a list replaces them
    wholesale (same shape as the global feature-models pref)."""
    feature_image_profiles: list[FeatureImageProfileOverridePayload] | None = None
    """Per-character image-profile overrides. Same tri-state semantics
    as ``feature_models``."""
    feature_video_profiles: list[FeatureVideoProfileOverridePayload] | None = None
    """Per-character video-profile overrides."""
    voice_profile: VoiceProfilePayload | None = None
    """Per-character TTS override. Omit = leave existing alone. Send
    a payload with all blank fields + ``enabled=true`` to clear the
    override (back to global defaults)."""
    companions: list[CharacterCompanionPayload] | None = None
    """私人 NPC 同伴清單。``None`` (omitted) = 不動既有同伴；``[]`` =
    清空所有同伴；非空 list = 全替換。每個元素的 ``id`` 若給就會被
    保留，未給的會由 domain 自動 mint 新 UUID。"""
    disposition: CharacterDispositionPayload | None = None
    """內在動機傾向四維。``None``（omitted）= 不動既有設定；非 None
    payload = 全替換。任何維度送 ``""`` 會被 normalise 成 ``"medium"``。"""
    body_state: BodyStatePayload | None = None
    """HUMANIZATION_ROADMAP §4.1 — 具身訊號四維。``None`` (omitted) = 不動；
    非 None payload 全替換；任何維度送 ``""`` 會被 normalise 成 ``"low"``。"""
    operator_pace_preference: str | None = None
    """HUMANIZATION_ROADMAP §3.6 — 對話節奏偏好。``None``（omitted）= 不動；
    傳合法字串或空字串會覆寫；未知值會被 normalise 為空字串。"""
    personality_type: CharacterPersonalityTypePayload | None = None
    """16 型性格設定。``None`` (omitted) = 不動；送 payload = 全替換；
    payload.code="" = 清除設定。"""


class CharacterResponse(BaseModel):
    id: str
    name: str
    summary: str
    personality: list[str]
    interests: list[str]
    speaking_style: str
    boundaries: list[str]
    aspirations: list[str]
    appearance: str
    gender_identity: str = ""
    third_person_pronoun: str = ""
    visual_gender_presentation: str = ""
    visual_subject_type: VisualSubjectType = DEFAULT_VISUAL_SUBJECT_TYPE
    visual_generation_style: str = ""
    date_of_birth: date | None = None
    image_urls: list[str]
    allowed_tools: list[str]
    loras: list[CharacterLoraPayload]
    state: CharacterStatePayload
    proactive_enabled: bool
    proactive_daily_limit: int
    proactive_cooldown_minutes: int
    proactive_rhythm: ProactiveRhythm
    feed_daily_limit: int
    world_awareness_enabled: bool
    world_topics: list[str]
    subscribed_categories: list[str] = Field(default_factory=list)
    excluded_topics: list[str] = Field(default_factory=list)
    world_frame: str
    accepts_web_proactive: bool
    unread_proactive_count: int
    unread_feed_reply_count: int = 0
    arc_template_id: str | None = None
    arc_series_id: str | None = None
    feature_models: list[FeatureModelOverridePayload] = Field(default_factory=list)
    feature_image_profiles: list[FeatureImageProfileOverridePayload] = Field(
        default_factory=list,
    )
    feature_video_profiles: list[FeatureVideoProfileOverridePayload] = Field(
        default_factory=list,
    )
    voice_profile: VoiceProfilePayload | None = None
    companions: list[CharacterCompanionPayload] = Field(default_factory=list)
    disposition: CharacterDispositionPayload = Field(
        default_factory=CharacterDispositionPayload,
    )
    body_state: BodyStatePayload = Field(default_factory=BodyStatePayload)
    operator_pace_preference: str = ""
    personality_type: CharacterPersonalityTypePayload = Field(
        default_factory=CharacterPersonalityTypePayload,
    )

    @classmethod
    def from_domain(cls, character: Character) -> "CharacterResponse":
        return cls(
            id=character.id,
            name=character.name,
            summary=character.summary,
            personality=character.personality,
            interests=character.interests,
            speaking_style=character.speaking_style,
            boundaries=character.boundaries,
            aspirations=character.aspirations,
            appearance=character.appearance,
            gender_identity=character.gender_identity,
            third_person_pronoun=character.third_person_pronoun,
            visual_gender_presentation=character.visual_gender_presentation,
            visual_subject_type=normalise_visual_subject_type(
                character.visual_subject_type,
            ),
            visual_generation_style=normalise_character_visual_generation_style(
                character.visual_generation_style,
            ),
            date_of_birth=character.date_of_birth,
            image_urls=list(character.image_urls),
            allowed_tools=list(character.allowed_tools),
            loras=[CharacterLoraPayload.from_domain(l) for l in character.loras],
            state=state_to_payload(character.state),
            proactive_enabled=character.proactive_enabled,
            proactive_daily_limit=character.proactive_daily_limit,
            proactive_cooldown_minutes=character.proactive_cooldown_minutes,
            proactive_rhythm=proactive_rhythm_from_values(
                daily_limit=character.proactive_daily_limit,
                cooldown_minutes=character.proactive_cooldown_minutes,
            ),
            feed_daily_limit=character.feed_daily_limit,
            world_awareness_enabled=character.world_awareness_enabled,
            world_topics=list(character.world_topics),
            subscribed_categories=list(character.subscribed_categories),
            excluded_topics=list(character.excluded_topics),
            world_frame=character.world_frame or "modern",
            accepts_web_proactive=character.accepts_web_proactive,
            unread_proactive_count=character.unread_proactive_count,
            unread_feed_reply_count=character.unread_feed_reply_count,
            arc_template_id=character.arc_template_id,
            arc_series_id=character.arc_series_id,
            feature_models=[
                FeatureModelOverridePayload.from_domain(entry)
                for entry in character.feature_models
            ],
            feature_image_profiles=[
                FeatureImageProfileOverridePayload.from_domain(entry)
                for entry in character.feature_image_profiles
            ],
            feature_video_profiles=[
                FeatureVideoProfileOverridePayload.from_domain(entry)
                for entry in character.feature_video_profiles
            ],
            voice_profile=(
                VoiceProfilePayload.from_domain(character.voice_profile)
                if character.voice_profile is not None else None
            ),
            companions=[
                CharacterCompanionPayload.from_domain(c)
                for c in character.companions
            ],
            disposition=CharacterDispositionPayload.from_domain(
                character.disposition,
            ),
            body_state=BodyStatePayload.from_domain(character.body_state),
            operator_pace_preference=character.operator_pace_preference,
            personality_type=CharacterPersonalityTypePayload.from_domain(
                character.personality_type,
            ),
        )


class GeneratePortraitRequest(BaseModel):
    """Body for ``POST /characters/{id}/images/generate``.

    ``positive`` is a danbooru-style tag string describing the scene /
    pose to render. Character ``appearance`` / visual gender
    presentation + current ``emotion`` are auto-prepended by the
    generator so the operator can keep this payload short
    (e.g. ``"cafe, reading a book, warm light"``).
    """

    positive: str
    aspect: str = "portrait"


class GenerateCandidatesRequest(BaseModel):
    """Body for ``POST /characters/{id}/images/candidates``.

    Same fields as :class:`GeneratePortraitRequest` plus ``count`` —
    number of candidate images to render in a single batch. The
    service caps the count; operator-provided values over the limit
    are clamped silently.
    """

    positive: str
    aspect: str = "portrait"
    count: int = 4


class GenerateCandidatesResponse(BaseModel):
    character_id: str
    candidates: list[str]
    """URLs of the freshly-generated candidate images. They live
    under ``/uploads/characters/{id}/candidates/`` until the operator
    commits (moves to permanent) or discards."""


class CommitCandidatesRequest(BaseModel):
    """Body for ``POST /characters/{id}/images/candidates/commit``.

    Two destination buckets:

    - ``keep_urls`` — candidates to promote into the stage carousel
      (appended to ``Character.image_urls``; subject to the 12-slot cap).
    - ``album_urls`` — candidates to send **directly** into the album,
      bypassing the stage. Useful for "this is a fun scene but I don't
      want it in rotation" picks.

    Anything in the candidates directory *not* listed in either bucket
    gets deleted. ``keep_urls=[], album_urls=[]`` → discard everything.
    URLs appearing in both lists are treated as stage picks.
    """

    keep_urls: list[str] = []
    album_urls: list[str] = []


class ResetCharacterDataRequest(BaseModel):
    """Body for ``POST /characters/{id}/reset``.

    Each flag scopes *one* datastore owned by the character. Operator
    typically ticks ``memories`` + ``conversations`` before editing the
    character's name / personality to sidestep identity drift; the flags
    are independent so a lighter reset (just memories, keep chat log as
    conversation context) is also possible.
    """

    memories: bool = False
    conversations: bool = False
    state_history: bool = False
    operator_persona: bool = False


class ResetCharacterDataResponse(BaseModel):
    character_id: str
    memories_deleted: int
    conversations_deleted: int
    state_history_deleted: int
    operator_persona_deleted: int = 0


class StateSnapshotResponse(BaseModel):
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

    @classmethod
    def from_domain(cls, snapshot: StateSnapshot) -> "StateSnapshotResponse":
        return cls(
            id=snapshot.id,
            character_id=snapshot.character_id,
            source=snapshot.source,
            emotion=snapshot.emotion,
            affection=snapshot.affection,
            fatigue=snapshot.fatigue,
            trust=snapshot.trust,
            energy=snapshot.energy,
            created_at=snapshot.created_at,
            trigger=snapshot.trigger,
        )


def payload_to_state(payload: CharacterStatePayload) -> CharacterState:
    return CharacterState(
        emotion=payload.emotion,
        affection=payload.affection,
        fatigue=payload.fatigue,
        trust=payload.trust,
        energy=payload.energy,
        last_active_at=payload.last_active_at,
        current_intent=payload.current_intent,
    )


def state_to_payload(state: CharacterState) -> CharacterStatePayload:
    return CharacterStatePayload(
        emotion=state.emotion,
        affection=state.affection,
        fatigue=state.fatigue,
        trust=state.trust,
        energy=state.energy,
        last_active_at=state.last_active_at,
        current_intent=state.current_intent,
    )
