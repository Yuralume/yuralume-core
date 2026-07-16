from dataclasses import dataclass, field, replace
from datetime import date, datetime
from uuid import uuid4

from kokoro_link.contracts.image_profile import FeatureImageProfileOverride
from kokoro_link.contracts.video_profile import FeatureVideoProfileOverride
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.domain.value_objects.birthday import BirthdayContext
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.companion import CharacterCompanion
from kokoro_link.domain.value_objects.body_state import BodyState
from kokoro_link.domain.value_objects.disposition import CharacterDisposition
from kokoro_link.domain.value_objects.personality_type import (
    CharacterPersonalityType,
)
from kokoro_link.domain.value_objects.visual_subject import (
    DEFAULT_VISUAL_SUBJECT_TYPE,
    VisualSubjectType,
    normalise_visual_subject_type,
)
from kokoro_link.domain.value_objects.visual_generation_style import (
    normalise_character_visual_generation_style,
)


def _body_state_default() -> BodyState:
    return BodyState.DEFAULT
from kokoro_link.domain.value_objects.voice_profile import VoiceProfile


# Site-level freeze provenance (CHARACTER_FREEZE_PLAN).
# The reason a character was frozen decides how it can be thawed:
#   - ``idle``              → auto-sweep reaper; foreground chat auto-unfreezes.
#   - ``manual``            → admin console; sticky (chat does not undo it).
#   - ``subscription_lapse``→ legacy pre-migration Cloud billing provenance;
#                             new billing locks use the orthogonal tenant state.
# ``None`` (legacy freezes with no recorded provenance) is treated as a soft
# freeze equivalent to ``idle`` for thaw purposes.
FREEZE_REASON_IDLE = "idle"
FREEZE_REASON_MANUAL = "manual"
FREEZE_REASON_SUBSCRIPTION_LAPSE = "subscription_lapse"

# Freeze reasons that a user's foreground chat is allowed to auto-thaw.
# ``subscription_lapse`` remains excluded for migrated/legacy rows; ``manual``
# is excluded so chat never silently undoes an admin action. Current billing
# authorization is enforced by ``SubscriptionAccessGuard``.
CHAT_THAWABLE_FREEZE_REASONS: frozenset[str | None] = frozenset(
    {FREEZE_REASON_IDLE, None},
)


@dataclass(frozen=True, slots=True)
class FeatureModelOverride:
    """Per-character LLM override for a single feature key.

    ``feature_key`` matches the constants in
    ``kokoro_link.application.services.feature_keys`` (e.g. ``"chat"``,
    ``"post_turn"``). ``provider_id`` / ``model_id`` follow the same
    blank-means-fallback semantics as the global ``feature_models``
    preference: blank ``provider_id`` falls back to the global picker;
    blank ``model_id`` lets the provider pick its own default.

    Stored as a tuple of these on ``Character`` so the entity stays
    copy-on-write hashable like the rest of its fields."""

    feature_key: str
    provider_id: str | None = None
    model_id: str | None = None

    def __post_init__(self) -> None:
        key = (self.feature_key or "").strip()
        if not key:
            raise ValueError("FeatureModelOverride.feature_key must be non-empty")
        object.__setattr__(self, "feature_key", key)
        provider = (self.provider_id or "").strip() or None
        object.__setattr__(self, "provider_id", provider)
        model = (self.model_id or "").strip() or None
        object.__setattr__(self, "model_id", model)

    @property
    def is_empty(self) -> bool:
        """``True`` when both fields are blank — caller should drop the
        entry rather than store a no-op override."""
        return self.provider_id is None and self.model_id is None


@dataclass(frozen=True, slots=True)
class CharacterLora:
    """LoRA weight reference to apply when generating images.

    ``name`` is a filename under the ComfyUI ``models/loras/``
    directory (no path, no extension stripping — e.g.
    ``"PrincessConnect_Kokkoro_IlluXL.safetensors"``). Multiple LoRAs
    are chained model-then-clip in order, with per-LoRA strength.

    ``strength`` applies to both the model and CLIP paths (we expose
    one knob for simplicity — 95% of LoRAs use matching values).
    Range is soft [0.0, 2.0]; negative values are legal in ComfyUI
    but we clamp at zero so misconfiguration can't silently invert
    a LoRA.
    """

    name: str
    strength: float = 1.0

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("CharacterLora.name must be non-empty")
        object.__setattr__(self, "name", self.name.strip())
        if self.strength < 0.0:
            object.__setattr__(self, "strength", 0.0)
        elif self.strength > 2.0:
            object.__setattr__(self, "strength", 2.0)

_DEFAULT_PROACTIVE_DAILY_LIMIT = 3
_DEFAULT_PROACTIVE_COOLDOWN_MINUTES = 30
_DEFAULT_FEED_DAILY_LIMIT = 3

# Sentinel used by ``Character.update`` to tell "caller did not pass
# arc_template_id" apart from "caller wants to clear the binding to
# None". A string id sets the binding; ``None`` clears it; the sentinel
# leaves the existing value alone.
_UNSET_TEMPLATE_ID: object = object()
_UNSET_SERIES_ID: object = object()

# Same tri-state sentinel for ``date_of_birth`` — operator needs to be
# able to clear a previously-set birthday back to "unknown" without the
# absence of the field meaning "clear me".
_UNSET_DOB: object = object()

DEFAULT_ALLOWED_TOOLS: tuple[str, ...] = (
    "generate_image",
    "web_fetch",
    "web_search",
)
"""Production tool allow-list granted to new characters by default.

The registry silently ignores unavailable adapters (for example
``web_search`` when Tavily is not configured), so this can name the
full production catalogue without forcing every deployment to enable
every integration.
"""


@dataclass(frozen=True, slots=True)
class Character:
    id: str
    name: str
    summary: str
    user_id: str = field(default=DEFAULT_OPERATOR_ID, kw_only=True)
    """Owner — the ``operator_profiles.id`` of the user who owns this
    character (MULTI_USER_AUTH_PLAN Batch 1). Default value matches the
    single-user install's pre-multi-user behaviour; new characters
    created via :meth:`Character.create` should pass the current user's
    id explicitly. Frozen / hashable like every other field."""
    personality: list[str]
    interests: list[str]
    speaking_style: str
    boundaries: list[str]
    state: CharacterState
    aspirations: list[str] = field(default_factory=list)
    appearance: str = ""
    gender_identity: str = ""
    """Free-form character gender identity / self-description.

    Empty string means unknown / unset. This is a character fact,
    separate from the operator profile's pronouns and separate from
    visual presentation so prompts and UI do not have to guess from the
    character name or appearance text.
    """
    third_person_pronoun: str = ""
    """Free-form third-person pronoun used to refer to this character.

    Examples include ``"他"``, ``"她"``, ``"TA"``, ``"它"`` or
    ``"they"``. Empty string means unknown / unset; callers should fall
    back to the character name or neutral copy rather than inferring
    gender.
    """
    visual_gender_presentation: str = ""
    """Free-form visual gender presentation for media generation.

    This intentionally does not have to match the grammatical pronoun:
    a user may want feminine pronouns with an androgynous appearance, or
    a genderless AI with a masculine visual design.
    """
    visual_subject_type: VisualSubjectType = DEFAULT_VISUAL_SUBJECT_TYPE
    """Visual body-plan hint for media generation.

    ``"auto"`` preserves legacy behaviour unless the media prompt
    strategy can safely infer a non-human animal from appearance text.
    Explicit values let operators prevent pets, creatures, or objects
    from being rendered as ordinary human portraits.
    """
    visual_generation_style: str = ""
    """Per-character image style override for generated images.

    ``""`` means inherit the existing user/global visual generation style
    preference. Explicit values are ``"anime"`` or ``"realistic"``.
    """
    date_of_birth: date | None = None
    """角色的出生日期。``None`` = 未設定（行為與舊資料相容，所有
    生日相關提示與動態都會略過）。設定後，``age`` / 星座 / 距離下次
    生日的天數都會從這欄即時推導（不快取），避免「過了一年但年齡
    沒更新」的退化。日期解讀為角色所處的世界時區的當地日期；目前
    系統只有一個世界時區，因此直接用 ``datetime.date`` 即可。"""
    image_urls: tuple[str, ...] = field(default_factory=tuple)
    allowed_tools: tuple[str, ...] = field(
        default_factory=lambda: DEFAULT_ALLOWED_TOOLS,
    )
    """Names of tools this character may invoke.

    Tool names must match the ``ToolPort.name`` of a registered tool;
    unknown names are ignored at runtime. Empty is still a deliberate
    "no tools" override, but new characters default to the production
    catalogue in :data:`DEFAULT_ALLOWED_TOOLS`. Kept as a tuple so the
    entity stays hashable / copy-on-write like the rest of the fields."""
    loras: tuple[CharacterLora, ...] = field(default_factory=tuple)
    """LoRA weights applied during image generation.

    Applied in declared order by ``ComfyImageTool`` — the workflow
    builder inserts a ``LoraLoader`` node per entry and chains them
    between the checkpoint loader and the sampler / CLIP encoders.
    Empty tuple → no LoRAs, plain checkpoint only (current default)."""
    """URLs of reference images that represent the character visually.

    Stored as an ordered tuple so the first image is the canonical
    portrait and the rest are alternates (cross-faded on the stage).
    URLs are relative paths under ``/uploads/`` served by the backend's
    static handler."""
    proactive_enabled: bool = True
    """When true, the proactive scheduler will consider this character
    for unprompted messages. Defaults to on for new characters; users can
    opt out per character from the player settings page."""
    proactive_daily_limit: int = _DEFAULT_PROACTIVE_DAILY_LIMIT
    """Max successful proactive sends per local day before the gate
    starts dropping further attempts."""
    proactive_cooldown_minutes: int = _DEFAULT_PROACTIVE_COOLDOWN_MINUTES
    """Minimum gap (minutes) between any two proactive evaluations
    going past the gate for the same character."""
    world_awareness_enabled: bool = False
    """When true, the chat prompt includes a ``最近世界上的事`` section
    with a few recent external events ranked against the user's message.

    Defaults off so fantasy / period / purely fictional personas stay in
    their bubble by default. Modern / curious personas should be toggled
    on via the settings UI."""
    world_topics: tuple[str, ...] = field(default_factory=tuple)
    """Optional topic filter. Empty = accept any topic. Non-empty
    narrows the selector to events whose ``topic_tags`` intersect this
    list (OR-match). Useful for a tech-focused character who should not
    see political feed items."""
    subscribed_categories: tuple[str, ...] = field(default_factory=tuple)
    """Optional ``RssCategory`` allow-list for the per-character event
    inbox curator. Empty = consider every enabled source's category.
    Non-empty = only events whose source category is in this list are
    eligible. Coarse pre-filter; embedding similarity then ranks the
    survivors. Free-form strings so unknown values round-trip cleanly
    (matches the ``RssCategory`` VO idiom)."""
    excluded_topics: tuple[str, ...] = field(default_factory=tuple)
    """Free-form topics the operator wants this character to avoid
    (e.g. ``("政治", "醜聞")``). Each entry is embedded once and the
    curator drops candidate events whose max cosine to any excluded
    vector exceeds an exclusion threshold. Empty = no exclusion
    filter."""
    world_frame: str = "modern"
    """Which fictional frame this character inhabits. Decides which
    ``StorySeed`` pool the gacha service can draw from (e.g. a fantasy
    character doesn't get seeds about checking social media) and —
    transitively — whether the RSS-backed world-event pool is
    appropriate. Free-form string; seeds declare which frames they
    support (``any`` / ``modern`` / ``fantasy`` / ``school`` / ...)."""
    accepts_web_proactive: bool = True
    """When true, proactive pushes without (or in addition to) a
    TG/LINE binding are delivered to the web conversation — the
    assistant message gets appended to ``source="web"`` and an event is
    published on the in-process bus so open browser sessions pick it up
    via SSE. Defaults on because a character who opted into proactive
    messaging (``proactive_enabled=True``) almost always wants *some*
    channel; if the operator only wants TG/LINE they can flip this off.
    """
    unread_proactive_count: int = 0
    """Number of unread proactive assistant messages in this character's
    web conversation. Incremented by the dispatcher's web delivery path;
    zeroed by ``POST /characters/{id}/conversations/mark-read`` when the
    user opens the chat. Drives the red dot on the sidebar avatar."""
    unread_feed_reply_count: int = 0
    """Number of unread LumeGram comment replies the character has
    posted at the user since the last ``POST /characters/{id}/feed/seen``.
    Incremented by ``FeedCommentReplyService`` on each scheduler-tick
    reply; zeroed on the same endpoint that already memorialises feed
    reactions (one user action — opening LumeGram — covers both
    "I've read what you said to me" and "I've seen your replies to me").
    Drives the red dot on the StagePage LumeGram launcher icon."""
    arc_template_id: str | None = None
    """Hand-written arc template (Phase 2 of SCENE_BEAT_PLAN) the next
    new arc for this character should materialise from.

    ``None`` (default) → arc planner falls back to LLM ``plan_arc``;
    same behaviour as before Phase 2 landed.

    A string id (e.g. ``"cafe_idol_audition"``) → ``StoryArcService``
    asks the YAML repository for the template and materialises it
    instead of calling the LLM. If the id can't be resolved (template
    file removed, typo) the service falls back to LLM planning + logs
    a warning so the operator can fix it."""
    arc_series_id: str | None = None
    """Authored multi-template series this character follows.

    ``None`` preserves legacy single-template / LLM-planned arc
    behaviour. A string id points to an ``ArcSeries`` whose member
    templates are materialised one at a time by ``StoryArcService``.
    """
    feed_daily_limit: int = _DEFAULT_FEED_DAILY_LIMIT
    """Cap on autonomous feed-wall posts per civil day. Independent
    from ``proactive_daily_limit`` because feed posts and IM-style
    proactive sends serve different purposes (passive browse vs.
    push). 0 = feed disabled for this character."""
    voice_profile: VoiceProfile | None = None
    """Per-character TTS override. ``None`` = use the global
    :class:`TTSSettings` for this character (zero-config opt-out).
    A non-``None`` profile lets each character have their own voice
    (different fine-tuned model, ref audio, prompt, dubbing target).
    See :class:`kokoro_link.domain.value_objects.voice_profile.VoiceProfile`
    for the field-level fallback rules."""
    feature_video_profiles: tuple[FeatureVideoProfileOverride, ...] = field(
        default_factory=tuple,
    )
    """Per-character video-profile routing overrides. Same shape as
    :attr:`feature_image_profiles` for the video side (Wan2.2 / future
    backends). Empty tuple = inherit from the global pref chain."""

    feature_image_profiles: tuple[FeatureImageProfileOverride, ...] = field(
        default_factory=tuple,
    )
    """Per-character image-profile routing overrides.

    Mirrors :attr:`feature_models` but for the image-generation side.
    Empty tuple = no overrides; every image feature key falls through
    to the global ``image_feature_profiles`` preference, then the
    global ``active_image_profile``, then the first registered
    profile. Lets operators wire e.g. character A to the anime
    ComfyUI profile and character B to a realistic Pony profile.

    Stored as a tuple of frozen ``FeatureImageProfileOverride`` so the
    entity stays copy-on-write hashable. Lookups go through
    :meth:`feature_image_profile_for`."""

    companions: tuple[CharacterCompanion, ...] = field(default_factory=tuple)
    """私人 NPC 同伴清單 —— 出現在角色生活圈裡的配角，會被注入到
    schedule planner / chat prompt 讓角色不再一直唱獨角戲。詳見
    :class:`kokoro_link.domain.value_objects.companion.CharacterCompanion`。
    Empty tuple 是預設「沒有同伴」狀態，舊資料不需要 backfill。"""

    operator_pace_preference: str = ""
    """Operator-facing dialogue-pace preference (HUMANIZATION_ROADMAP §3.6).

    Stored verbatim as one of ``""`` (unset — no injection),
    ``"more_active"`` (使用者希望角色更主動 / 更話多),
    ``"balanced"`` (預設節奏),
    ``"more_quiet"`` (使用者希望角色更節制 / 偏留白). Per-character
    because each character × operator pair may want a different rhythm
    even in the single-operator world we live in today; future
    multi-operator migrations can split this onto ``OperatorPersona``
    without changing prompt-side consumers."""

    disposition: CharacterDisposition = field(
        default_factory=lambda: CharacterDisposition.DEFAULT,
    )
    """內在動機傾向（四維 qualitative band）—— 影響 chat / proactive
    prompt 的事實層描述，**禁止**在程式分支條件中讀。預設為全 medium
    （即 ``CharacterDisposition.DEFAULT``），等同「沒設定」，prompt
    builder 會 ``to_prompt_lines`` 回空 list 自動跳過。詳見
    :class:`kokoro_link.domain.value_objects.disposition.CharacterDisposition`。"""

    body_state: "BodyState" = field(
        default_factory=lambda: _body_state_default(),
    )
    """具身訊號（HUMANIZATION_ROADMAP §4.1，四維 qualitative band）——
    hunger / thirst / sleep_debt / seasonal_allergy。預設全 low（沒任何
    不適），prompt builder 會跳過渲染。owner 決議（2026-05-21）月經週期
    相位不做。詳見 :class:`kokoro_link.domain.value_objects.body_state.BodyState`。"""

    personality_type: CharacterPersonalityType = field(
        default_factory=lambda: CharacterPersonalityType.DEFAULT,  # type: ignore[attr-defined]
    )
    """角色的 16 型性格創作參考。屬於角色 A-layer 靜態設定，可進角色卡
    與 prompt，但禁止進入程式分支條件。未設定時 ``code=""``，prompt
    renderer 會自動跳過。"""

    feature_models: tuple[FeatureModelOverride, ...] = field(default_factory=tuple)
    """Per-character LLM routing overrides.

    Empty tuple = no overrides; every feature falls through to the
    global ``feature_models`` preference, then ``active_model``, then
    the container default. A non-empty entry takes precedence over the
    global pick for that one feature key, including ``"chat"`` (the
    main reply path) — letting operators wire e.g. character A to
    Anthropic Sonnet while character B keeps using LM Studio.

    Stored as a tuple of frozen ``FeatureModelOverride`` value objects
    rather than a dict so the entity stays copy-on-write hashable like
    the rest of its fields. Lookups go through
    :meth:`feature_model_for`, which is O(n) on a small list (≤10
    entries in practice)."""

    frozen: bool = False
    """Site-level cost-control freeze (CHARACTER_FREEZE_PLAN).

    When ``True`` the character keeps all of its persisted state but the
    background scheduler skips **every** unprompted activity for it —
    proactive pings, feed composition, schedule generation,
    memorialization, encounters, peer-knowledge / persona-dream
    consolidation, rest recovery. This is a strictly broader off-switch
    than :attr:`proactive_enabled` (which only silences proactive pings
    while feed / schedule / encounters keep running). Foreground chat is
    unaffected: a user message auto-unfreezes the character (the chat
    path clears this flag), so freezing is a pure dormancy / cost knob,
    not a lock. Set by the admin console (immediate freeze) or the
    idle-sweep reaper (auto-freeze after N days of no interaction)."""

    frozen_at: datetime | None = None
    """When the character was frozen (audit / admin display). ``None``
    when not frozen. Cleared back to ``None`` on unfreeze."""

    frozen_reason: str | None = None
    """Provenance of the current freeze — normally ``idle`` / ``manual``.

    Legacy rows may still contain ``subscription_lapse`` until migration
    normalizes them (see the module-level ``FREEZE_REASON_*`` constants).
    Decides how this independent freeze may be thawed: only
    :data:`CHAT_THAWABLE_FREEZE_REASONS` (``idle`` / ``None``) auto-thaw on
    a user chat turn; legacy ``subscription_lapse`` and ``manual`` remain
    sticky. Current Cloud billing access uses the tenant repository/guard.
    ``None`` when not frozen, and on legacy freezes that predate this
    column (treated as a soft/idle freeze)."""

    subscription_locked: bool = False
    """Retryable projection of the owning Cloud tenant subscription lock.

    It is orthogonal to idle/manual frozen provenance and is never the
    authorization source of truth. The tenant state repository is
    authoritative; this projection only lets background scans cheaply skip
    locked characters and is owned by a dedicated update."""

    created_at: datetime | None = None
    """Row creation instant — **read-only** projection of the DB
    ``characters.created_at`` column. Populated by the repository on
    load and ignored on save (the column is server-managed). Used by the
    auto-freeze reaper as the idle anchor for characters the user has
    never chatted with (``state.last_active_at is None``). ``None`` on a
    freshly-constructed entity before it round-trips through the DB."""

    def feature_video_profile_for(
        self, feature_key: str,
    ) -> FeatureVideoProfileOverride | None:
        """Return the per-character video-profile override or ``None``.

        Same fall-through semantics as :meth:`feature_image_profile_for`."""
        if not feature_key:
            return None
        for entry in self.feature_video_profiles:
            if entry.feature_key == feature_key:
                return None if entry.is_empty else entry
        return None

    def feature_image_profile_for(
        self, feature_key: str,
    ) -> FeatureImageProfileOverride | None:
        """Return the per-character image-profile override for
        ``feature_key`` or ``None`` when nothing is pinned.

        ``None`` is the "no override" case; the active-image-provider
        resolver falls through to the global picks. An entry whose
        ``profile_id`` is blank is treated as absent."""
        if not feature_key:
            return None
        for entry in self.feature_image_profiles:
            if entry.feature_key == feature_key:
                return None if entry.is_empty else entry
        return None

    def feature_model_for(self, feature_key: str) -> FeatureModelOverride | None:
        """Return the override entry for ``feature_key`` or ``None``.

        ``None`` is the "no override" case; callers should fall through
        to global preferences. An entry whose ``provider_id`` and
        ``model_id`` are both blank is treated as absent so a stale
        all-null entry doesn't accidentally clear the global pick."""
        if not feature_key:
            return None
        for entry in self.feature_models:
            if entry.feature_key == feature_key:
                return None if entry.is_empty else entry
        return None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "gender_identity",
            _normalise_identity_text(self.gender_identity),
        )
        object.__setattr__(
            self,
            "third_person_pronoun",
            _normalise_identity_text(self.third_person_pronoun),
        )
        object.__setattr__(
            self,
            "visual_gender_presentation",
            _normalise_identity_text(self.visual_gender_presentation),
        )
        object.__setattr__(
            self,
            "visual_subject_type",
            normalise_visual_subject_type(self.visual_subject_type),
        )

    @classmethod
    def create(
        cls,
        *,
        name: str,
        summary: str,
        user_id: str = DEFAULT_OPERATOR_ID,
        personality: list[str],
        interests: list[str],
        speaking_style: str,
        boundaries: list[str],
        state: CharacterState,
        aspirations: list[str] | None = None,
        appearance: str = "",
        gender_identity: str = "",
        third_person_pronoun: str = "",
        visual_gender_presentation: str = "",
        visual_subject_type: str = DEFAULT_VISUAL_SUBJECT_TYPE,
        visual_generation_style: str = "",
        date_of_birth: date | None = None,
        image_urls: tuple[str, ...] | list[str] | None = None,
        allowed_tools: tuple[str, ...] | list[str] | None = None,
        loras: tuple[CharacterLora, ...] | list[CharacterLora] | None = None,
        proactive_enabled: bool = True,
        proactive_daily_limit: int = _DEFAULT_PROACTIVE_DAILY_LIMIT,
        proactive_cooldown_minutes: int = _DEFAULT_PROACTIVE_COOLDOWN_MINUTES,
        world_awareness_enabled: bool = False,
        world_topics: tuple[str, ...] | list[str] | None = None,
        subscribed_categories: tuple[str, ...] | list[str] | None = None,
        excluded_topics: tuple[str, ...] | list[str] | None = None,
        world_frame: str = "modern",
        accepts_web_proactive: bool = True,
        arc_template_id: str | None = None,
        arc_series_id: str | None = None,
        feature_models: tuple[FeatureModelOverride, ...] | list[FeatureModelOverride] | None = None,
        feature_image_profiles: tuple[FeatureImageProfileOverride, ...] | list[FeatureImageProfileOverride] | None = None,
        feature_video_profiles: tuple[FeatureVideoProfileOverride, ...] | list[FeatureVideoProfileOverride] | None = None,
        feed_daily_limit: int = _DEFAULT_FEED_DAILY_LIMIT,
        companions: tuple[CharacterCompanion, ...] | list[CharacterCompanion] | None = None,
        disposition: CharacterDisposition | None = None,
        body_state: BodyState | None = None,
        operator_pace_preference: str = "",
        personality_type: CharacterPersonalityType | None = None,
    ) -> "Character":
        return cls(
            id=str(uuid4()),
            name=name,
            summary=summary,
            user_id=user_id,
            personality=list(personality),
            interests=list(interests),
            speaking_style=speaking_style,
            boundaries=list(boundaries),
            state=state,
            aspirations=list(aspirations or []),
            appearance=appearance,
            gender_identity=gender_identity,
            third_person_pronoun=third_person_pronoun,
            visual_gender_presentation=visual_gender_presentation,
            visual_subject_type=normalise_visual_subject_type(
                visual_subject_type,
            ),
            visual_generation_style=normalise_character_visual_generation_style(
                visual_generation_style,
            ),
            date_of_birth=date_of_birth,
            image_urls=tuple(image_urls or ()),
            allowed_tools=(
                DEFAULT_ALLOWED_TOOLS
                if allowed_tools is None
                else tuple(allowed_tools)
            ),
            loras=tuple(loras or ()),
            proactive_enabled=proactive_enabled,
            proactive_daily_limit=_clamp_daily_limit(proactive_daily_limit),
            proactive_cooldown_minutes=_clamp_cooldown(proactive_cooldown_minutes),
            world_awareness_enabled=world_awareness_enabled,
            world_topics=tuple(world_topics or ()),
            subscribed_categories=_normalise_str_tuple(subscribed_categories, lower=True),
            excluded_topics=_normalise_str_tuple(excluded_topics),
            world_frame=(world_frame or "modern").strip() or "modern",
            accepts_web_proactive=accepts_web_proactive,
            unread_proactive_count=0,
            arc_template_id=_normalise_template_id(arc_template_id),
            arc_series_id=_normalise_template_id(arc_series_id),
            feature_models=_normalise_feature_models(feature_models),
            feature_image_profiles=_normalise_feature_image_profiles(
                feature_image_profiles,
            ),
            feature_video_profiles=_normalise_feature_video_profiles(
                feature_video_profiles,
            ),
            feed_daily_limit=_clamp_feed_daily_limit(feed_daily_limit),
            companions=_normalise_companions(companions),
            disposition=disposition or CharacterDisposition.DEFAULT,
            body_state=body_state or BodyState.DEFAULT,
            operator_pace_preference=_normalise_pace_preference(
                operator_pace_preference,
            ),
            personality_type=personality_type or CharacterPersonalityType.DEFAULT,  # type: ignore[attr-defined]
        )

    def update(
        self,
        *,
        name: str | None,
        summary: str | None,
        personality: list[str] | None,
        interests: list[str] | None,
        speaking_style: str | None,
        boundaries: list[str] | None,
        state: CharacterState | None,
        aspirations: list[str] | None = None,
        appearance: str | None = None,
        gender_identity: str | None = None,
        third_person_pronoun: str | None = None,
        visual_gender_presentation: str | None = None,
        visual_subject_type: str | None = None,
        visual_generation_style: str | None = None,
        date_of_birth: date | None = _UNSET_DOB,  # type: ignore[assignment]
        image_urls: tuple[str, ...] | list[str] | None = None,
        allowed_tools: tuple[str, ...] | list[str] | None = None,
        loras: tuple[CharacterLora, ...] | list[CharacterLora] | None = None,
        proactive_enabled: bool | None = None,
        proactive_daily_limit: int | None = None,
        proactive_cooldown_minutes: int | None = None,
        world_awareness_enabled: bool | None = None,
        world_topics: tuple[str, ...] | list[str] | None = None,
        subscribed_categories: tuple[str, ...] | list[str] | None = None,
        excluded_topics: tuple[str, ...] | list[str] | None = None,
        world_frame: str | None = None,
        accepts_web_proactive: bool | None = None,
        arc_template_id: str | None = _UNSET_TEMPLATE_ID,
        arc_series_id: str | None = _UNSET_SERIES_ID,
        feature_models: tuple[FeatureModelOverride, ...] | list[FeatureModelOverride] | None = None,
        feature_image_profiles: tuple[FeatureImageProfileOverride, ...] | list[FeatureImageProfileOverride] | None = None,
        feature_video_profiles: tuple[FeatureVideoProfileOverride, ...] | list[FeatureVideoProfileOverride] | None = None,
        feed_daily_limit: int | None = None,
        companions: tuple[CharacterCompanion, ...] | list[CharacterCompanion] | None = None,
        disposition: CharacterDisposition | None = None,
        body_state: BodyState | None = None,
        operator_pace_preference: str | None = None,
        personality_type: CharacterPersonalityType | None = None,
    ) -> "Character":
        # ``arc_template_id`` is genuinely tri-state: ``None`` = unbind
        # (clear the field), ``"xyz"`` = bind, *missing* = leave alone.
        # The default sentinel distinguishes "missing" from "unbind".
        return replace(
            self,
            name=self.name if name is None else name,
            summary=self.summary if summary is None else summary,
            personality=self.personality if personality is None else list(personality),
            interests=self.interests if interests is None else list(interests),
            speaking_style=self.speaking_style if speaking_style is None else speaking_style,
            boundaries=self.boundaries if boundaries is None else list(boundaries),
            state=self.state if state is None else state,
            aspirations=self.aspirations if aspirations is None else list(aspirations),
            appearance=self.appearance if appearance is None else appearance,
            gender_identity=(
                self.gender_identity if gender_identity is None
                else gender_identity
            ),
            third_person_pronoun=(
                self.third_person_pronoun if third_person_pronoun is None
                else third_person_pronoun
            ),
            visual_gender_presentation=(
                self.visual_gender_presentation
                if visual_gender_presentation is None
                else visual_gender_presentation
            ),
            visual_subject_type=(
                self.visual_subject_type if visual_subject_type is None
                else normalise_visual_subject_type(visual_subject_type)
            ),
            visual_generation_style=(
                self.visual_generation_style
                if visual_generation_style is None
                else normalise_character_visual_generation_style(
                    visual_generation_style,
                )
            ),
            date_of_birth=(
                self.date_of_birth
                if date_of_birth is _UNSET_DOB
                else date_of_birth
            ),
            image_urls=(
                self.image_urls if image_urls is None else tuple(image_urls)
            ),
            allowed_tools=(
                self.allowed_tools if allowed_tools is None else tuple(allowed_tools)
            ),
            loras=(self.loras if loras is None else tuple(loras)),
            proactive_enabled=(
                self.proactive_enabled if proactive_enabled is None else proactive_enabled
            ),
            proactive_daily_limit=(
                self.proactive_daily_limit if proactive_daily_limit is None
                else _clamp_daily_limit(proactive_daily_limit)
            ),
            proactive_cooldown_minutes=(
                self.proactive_cooldown_minutes if proactive_cooldown_minutes is None
                else _clamp_cooldown(proactive_cooldown_minutes)
            ),
            world_awareness_enabled=(
                self.world_awareness_enabled if world_awareness_enabled is None
                else world_awareness_enabled
            ),
            world_topics=(
                self.world_topics if world_topics is None else tuple(world_topics)
            ),
            subscribed_categories=(
                self.subscribed_categories if subscribed_categories is None
                else _normalise_str_tuple(subscribed_categories, lower=True)
            ),
            excluded_topics=(
                self.excluded_topics if excluded_topics is None
                else _normalise_str_tuple(excluded_topics)
            ),
            world_frame=(
                self.world_frame if world_frame is None
                else (world_frame.strip() or "modern")
            ),
            accepts_web_proactive=(
                self.accepts_web_proactive if accepts_web_proactive is None
                else accepts_web_proactive
            ),
            arc_template_id=(
                self.arc_template_id
                if arc_template_id is _UNSET_TEMPLATE_ID
                else _normalise_template_id(arc_template_id)
            ),
            arc_series_id=(
                self.arc_series_id
                if arc_series_id is _UNSET_SERIES_ID
                else _normalise_template_id(arc_series_id)
            ),
            feature_models=(
                self.feature_models if feature_models is None
                else _normalise_feature_models(feature_models)
            ),
            feature_image_profiles=(
                self.feature_image_profiles if feature_image_profiles is None
                else _normalise_feature_image_profiles(feature_image_profiles)
            ),
            feature_video_profiles=(
                self.feature_video_profiles if feature_video_profiles is None
                else _normalise_feature_video_profiles(feature_video_profiles)
            ),
            feed_daily_limit=(
                self.feed_daily_limit if feed_daily_limit is None
                else _clamp_feed_daily_limit(feed_daily_limit)
            ),
            companions=(
                self.companions if companions is None
                else _normalise_companions(companions)
            ),
            disposition=(
                self.disposition if disposition is None else disposition
            ),
            body_state=(
                self.body_state if body_state is None else body_state
            ),
            operator_pace_preference=(
                self.operator_pace_preference
                if operator_pace_preference is None
                else _normalise_pace_preference(operator_pace_preference)
            ),
            personality_type=(
                self.personality_type if personality_type is None
                else personality_type
            ),
        )

    def birthday_context(self, as_of: date) -> BirthdayContext | None:
        """Return a bundle of birthday-derived values for ``as_of``.

        ``None`` when the operator hasn't set ``date_of_birth`` — keeps
        callers from sprinkling ``if character.date_of_birth is None``
        checks at every consumption site, and signals "no birthday
        information, skip every downstream prompt / candidate hook".
        """
        if self.date_of_birth is None:
            return None
        return BirthdayContext.from_date(self.date_of_birth, as_of)

    def with_state(self, state: CharacterState) -> "Character":
        return replace(self, state=state)

    def with_unread_proactive(self, count: int) -> "Character":
        # Counter is non-negative; clamp so a stray decrement can't wedge
        # the badge into a negative state the UI can't render.
        return replace(self, unread_proactive_count=max(0, count))

    def with_unread_feed_reply(self, count: int) -> "Character":
        # Same non-negative clamp as the proactive counter — the badge
        # renders raw integers and a negative would break formatting.
        return replace(self, unread_feed_reply_count=max(0, count))

    def with_voice_profile(
        self, profile: VoiceProfile | None,
    ) -> "Character":
        # Empty profile collapses to ``None`` so the persistence layer
        # doesn't store a meaningless all-blank row.
        if profile is not None and profile.is_empty:
            profile = None
        return replace(self, voice_profile=profile)

    def with_image_urls(self, image_urls: tuple[str, ...] | list[str]) -> "Character":
        return replace(self, image_urls=tuple(image_urls))

    def with_loras(
        self, loras: tuple[CharacterLora, ...] | list[CharacterLora],
    ) -> "Character":
        return replace(self, loras=tuple(loras))


def _clamp_daily_limit(value: int) -> int:
    # Zero is "never send"; negative is meaningless. Cap at something
    # sane so a typo doesn't authorise hundreds per day.
    if value < 0:
        return 0
    if value > 50:
        return 50
    return value


def _clamp_feed_daily_limit(value: int) -> int:
    # 0 = feed disabled; mirrors ``_clamp_daily_limit`` semantics.
    if value < 0:
        return 0
    if value > 50:
        return 50
    return value


_PACE_PREFERENCES: frozenset[str] = frozenset({
    "", "more_active", "balanced", "more_quiet",
})


def _normalise_pace_preference(value: object) -> str:
    """Coerce operator pace preference to one of the valid string codes
    (HUMANIZATION_ROADMAP §3.6).

    ``""`` = unset; any unknown value is collapsed to ``""`` so a typo
    from the API never silently lands a bad enum in the DB."""
    if not isinstance(value, str):
        return ""
    cleaned = value.strip().lower()
    if cleaned not in _PACE_PREFERENCES:
        return ""
    return cleaned


def _normalise_identity_text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _clamp_cooldown(value: int) -> int:
    if value < 1:
        return 1
    if value > 24 * 60:
        return 24 * 60
    return value


def _normalise_feature_models(
    value: tuple[FeatureModelOverride, ...] | list[FeatureModelOverride] | None,
) -> tuple[FeatureModelOverride, ...]:
    """Drop blank entries and de-duplicate by feature_key (last wins).

    The DTO layer hands us whatever the operator typed, so we defend
    here against (a) all-null entries that would just bloat the row,
    and (b) duplicate keys that would make ``feature_model_for`` depend
    on insertion order. ``None`` is preserved for tests / migrations
    that legitimately want to clear the field."""
    if value is None:
        return ()
    by_key: dict[str, FeatureModelOverride] = {}
    for entry in value:
        if entry.is_empty:
            continue
        by_key[entry.feature_key] = entry
    return tuple(by_key.values())


def _normalise_feature_image_profiles(
    value: tuple[FeatureImageProfileOverride, ...]
    | list[FeatureImageProfileOverride]
    | None,
) -> tuple[FeatureImageProfileOverride, ...]:
    """Mirror of :func:`_normalise_feature_models` for image profile
    overrides — drop blank entries, de-dup by feature_key."""
    if value is None:
        return ()
    by_key: dict[str, FeatureImageProfileOverride] = {}
    for entry in value:
        if entry.is_empty:
            continue
        by_key[entry.feature_key] = entry
    return tuple(by_key.values())


def _normalise_feature_video_profiles(
    value: tuple[FeatureVideoProfileOverride, ...]
    | list[FeatureVideoProfileOverride]
    | None,
) -> tuple[FeatureVideoProfileOverride, ...]:
    """Mirror of :func:`_normalise_feature_image_profiles` for video."""
    if value is None:
        return ()
    by_key: dict[str, FeatureVideoProfileOverride] = {}
    for entry in value:
        if entry.is_empty:
            continue
        by_key[entry.feature_key] = entry
    return tuple(by_key.values())


def _normalise_str_tuple(
    value: tuple[str, ...] | list[str] | None,
    *,
    lower: bool = False,
) -> tuple[str, ...]:
    """Strip blanks, drop duplicates (preserving first-seen order).

    Used by tag-like list fields (``subscribed_categories`` /
    ``excluded_topics``) so the operator can paste sloppy input and
    still get a clean canonical tuple. ``lower=True`` lowercases each
    entry — appropriate for category enums, not for free-form topics
    that may legitimately be Chinese / Japanese."""
    if value is None:
        return ()
    seen: set[str] = set()
    out: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            continue
        cleaned = raw.strip()
        if lower:
            cleaned = cleaned.lower()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        out.append(cleaned)
    return tuple(out)


def _normalise_companions(
    value: tuple[CharacterCompanion, ...] | list[CharacterCompanion] | None,
) -> tuple[CharacterCompanion, ...]:
    """De-duplicate companions by id (last wins), drop invalid entries.

    Same defensive shape as :func:`_normalise_feature_models`: the DTO
    layer might hand us partially-typed dicts that got coerced, so we
    silently skip anything that isn't already a ``CharacterCompanion``
    (the DTO ``to_domain`` is responsible for that conversion). Cap
    at a small number so a runaway operator UI can't bloat the row."""
    if value is None:
        return ()
    by_id: dict[str, CharacterCompanion] = {}
    for entry in value:
        if not isinstance(entry, CharacterCompanion):
            continue
        by_id[entry.id] = entry
        if len(by_id) >= _MAX_COMPANIONS:
            break
    return tuple(by_id.values())


_MAX_COMPANIONS = 12
"""Cap on companions per character. Twelve is plenty — past that the
prompt context bloats faster than the model can usefully ground on
specific names. Operators wanting a larger cast probably want real
characters, not NPCs."""


def _normalise_template_id(value: object) -> str | None:
    """Treat blank / whitespace-only ids as "no binding".

    Avoids storing ``""`` in DB columns, which would later trip an
    ``await repo.get("")`` lookup that returns ``None`` anyway and
    confuses the post-mortem ("why is the template empty string?").
    """
    if value is None:
        return None
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    # Anything non-stringy means the caller passed a wrong type — don't
    # silently coerce, just unbind so the type contract stays clean.
    return None
