import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

_LOGGER = logging.getLogger(__name__)

# Env vars that migrated into Admin-DB config (CORE_ENV_TO_ADMIN_CONFIG).
# When any of these is still set we log a one-line deprecation notice at
# settings load: the value is still honoured as a first-boot seed, but the
# Admin UI is now the source of truth and the env is scheduled for removal
# one release later. Keyed by the canonical env name; alias names (e.g.
# ``KOKORO_`` prefixed twins) are listed alongside so either trips it.
_DEPRECATED_ENV_VARS: tuple[str, ...] = (
    # Track 1 — provider_settings (image / video / TTS / embedding / local LLM)
    "KOKORO_COMFYUI_SERVER",
    "KOKORO_COMFYUI_CHECKPOINT",
    "KOKORO_COMFYUI_WORKFLOW_FILE",
    "KOKORO_COMFYUI_TIMEOUT",
    "KOKORO_COMFYUI_LORA_DIR",
    "KOKORO_IMAGE_API_KEY",
    "KOKORO_IMAGE_API_BASE_URL",
    "KOKORO_IMAGE_API_MODEL",
    "KOKORO_IMAGE_API_PROVIDER",
    "KOKORO_VIDEO_API_KEY",
    "KOKORO_VIDEO_API_BASE_URL",
    "KOKORO_VIDEO_API_MODEL",
    "KOKORO_VIDEO_API_PROVIDER",
    "KOKORO_OPENAI_IMAGE_API_KEY",
    "KOKORO_OPENAI_IMAGE_BASE_URL",
    "KOKORO_OPENAI_IMAGE_MODEL",
    "KOKORO_EMBEDDING_MODEL",
    "EMBEDDING_MODEL",
    "KOKORO_TTS_API_KEY",
    "KOKORO_TTS_BASE_URL",
    "KOKORO_TTS_MODEL",
    "KOKORO_TTS_VOICE_ID",
    "KOKORO_TTS_TRANSLATE_TARGET_LANG",
    # Track 2 — app_runtime_settings (deployment facts / policy)
    "KOKORO_WEATHER_LATITUDE",
    "WEATHER_LATITUDE",
    "KOKORO_WEATHER_LONGITUDE",
    "WEATHER_LONGITUDE",
    "KOKORO_CALENDAR_REGION",
    "CALENDAR_REGION",
    "KOKORO_GEOIP_ENABLED",
    "GEOIP_ENABLED",
    "KOKORO_NSFW_MODE_TTL_SECONDS",
    "NSFW_MODE_TTL_SECONDS",
    # Track 3 — content management (RSS world-event feeds)
    "KOKORO_WORLD_EVENT_FEED_1_SOURCE_ID",
    "KOKORO_WORLD_EVENT_RETENTION_DAYS",
    "KOKORO_WORLD_EVENT_SCHEDULER_INTERVAL",
)


def _warn_deprecated_env() -> None:
    """Log a deprecation notice for any migrated env var still present.

    Idempotent per-process signalling is not needed — settings load runs
    once at startup; a self-host operator sees the notice in their logs
    and knows to move the value into the Admin UI. The env is still read
    (seeded on first boot), so behaviour is unchanged this release."""
    for name in _DEPRECATED_ENV_VARS:
        if os.getenv(name, "").strip():
            _LOGGER.warning(
                "deprecated: %s 已改由 Admin 後台管理，將於下個 release 移除"
                "（目前仍會在首次啟動時播種進 DB）",
                name,
            )

from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_PRIMARY_LANGUAGE,
    normalise_language_tag,
)
from kokoro_link.domain.value_objects.timezone import normalise_timezone_id

from kokoro_link.infrastructure.llm.openai_compatible import OpenAICompatibleChatModel


# Catalog of cloud LLM providers we ship zero-config adapters for.
# Each row = (provider_id, env-prefix, default base URL, default model).
# Registration is gated on ``KOKORO_{PREFIX}_API_KEY`` being set — any
# row without a key just doesn't appear in the registry, so ``fake`` and
# ``lmstudio`` remain usable without touching cloud config.
#
# Model defaults are the cheap/mid-tier pick each provider is best known
# for; operators override via ``KOKORO_{PREFIX}_MODEL`` or the per-turn
# model dropdown in the UI. ``base_url`` override is only useful for
# enterprise endpoints / regional mirrors (Azure OpenAI, Vertex mirror,
# Moonshot .cn) and is intentionally not documented in .env.example.
_OPENAI_COMPATIBLE_CLOUD_PROVIDERS: tuple[tuple[str, str, str, str], ...] = (
    ("openai",     "OPENAI",     "https://api.openai.com/v1",                      "gpt-4o-mini"),
    ("deepseek",   "DEEPSEEK",   "https://api.deepseek.com/v1",                    "deepseek-chat"),
    ("openrouter", "OPENROUTER", "https://openrouter.ai/api/v1",                   "openai/gpt-4o-mini"),
    ("gemini",     "GEMINI",     "https://generativelanguage.googleapis.com/v1beta/openai", "gemini-2.0-flash"),
    ("mistral",    "MISTRAL",    "https://api.mistral.ai/v1",                      "mistral-small-latest"),
)
"""Gemini uses Google's OpenAI-compatible endpoint (``/v1beta/openai/``)
rather than the native generativelanguage API — saves us writing a
separate adapter. Vision, streaming, and tool-calling all route through
the same ``/chat/completions`` path."""


@dataclass(frozen=True, slots=True)
class OpenAICompatibleProviderConfig:
    provider_id: str
    base_url: str
    api_key: str | None
    model: str
    max_tokens: int | None = None
    """Upper bound on completion tokens per request.

    Most OpenAI-compatible servers (LM Studio especially) default to a
    surprisingly low cap (~512 tokens) which truncates long JSON tool
    calls mid-argument. Setting this via
    ``KOKORO_LMSTUDIO_MAX_TOKENS`` (or the provider-specific env)
    forwards it as ``max_tokens`` on every request. ``None`` omits the
    field so the server's default applies."""


@dataclass(frozen=True, slots=True)
class EmbeddingSettings:
    """Config for the semantic-memory embedder.

    When ``model`` is empty, the null embedder is installed and semantic
    retrieval falls back to pure salience × recency ranking.
    """

    model: str = ""
    base_url: str = ""
    api_key: str | None = None
    dimension: int = 1024


@dataclass(frozen=True, slots=True)
class ObjectStorageSettings:
    """Config for user/generated media storage."""

    provider: str = "http"
    base_url: str = ""
    api_key: str = ""
    public_base_url: str = ""
    timeout_seconds: float = 30.0


@dataclass(frozen=True, slots=True)
class MediaApiSettings:
    """Endpoint/key/model config for hosted media APIs."""

    base_url: str = ""
    api_key: str = ""
    model: str = ""
    provider: str = "gateway"
    timeout_seconds: float = 180.0

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.api_key and self.model)


@dataclass(frozen=True, slots=True)
class ComfyUISettings:
    """ComfyUI image-generation tool config.

    Empty ``server`` means the ComfyImageTool is not installed — the
    fake image tool still works for dev / tests. ``checkpoint`` can be
    overridden per deployment if the default Illustrious weights don't
    exist.
    """

    server: str = ""
    checkpoint: str = "waiNSFWIllustrious_v140.safetensors"
    workflow_file: str = ""
    generation_timeout_seconds: float = 180.0
    lora_dir: str = ""
    """Filesystem path of ComfyUI's ``models/loras/`` directory.

    When set, our LoRA upload endpoint writes files there so ComfyUI
    can discover them by name. When empty, uploads are rejected — the
    operator can still reference LoRAs that were placed there manually
    (the character just stores the filename)."""

    @property
    def enabled(self) -> bool:
        return bool(self.server)


@dataclass(frozen=True, slots=True)
class OpenAIImageSettings:
    """OpenAI GPT Image 2 provider config.

    Hosted alternative to ComfyUI for image generation. Selected by
    ``KOKORO_IMAGE_PROVIDER=openai``; otherwise these knobs are inert.
    Empty ``api_key`` keeps the provider disabled even if the switch
    is flipped, so a misconfigured deploy degrades to "no image" cleanly
    rather than 401-ing every call.

    ``quality`` is one of ``low`` / ``medium`` / ``high`` / ``auto`` —
    drives both latency (~5–25 s) and cost (~$0.01–$0.10 per image)
    on OpenAI's side, so deployments size it for their budget.
    """

    api_key: str = ""
    model: str = "gpt-image-2"
    quality: str = "medium"
    timeout_seconds: float = 180.0
    base_url: str = "https://api.openai.com/v1"

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True, slots=True)
class TTSSettings:
    """External TTS capability service config.

    The core app sends text + ``voice_id`` to a stable API. The service behind
    that API may be OpenAI TTS, a custom voice service, or a GPT-SoVITS wrapper;
    this app no longer scans or understands those provider assets.

    For ``provider="openai"``, ``api_key`` is enough to enable direct
    speech synthesis. For ``api`` / ``custom`` providers, ``base_url``
    points to the external capability service.

    Legacy path fields are kept for backward-compatible character rows/tests.
    """

    provider: str = "api"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    voice_id: str = ""
    response_format: str = "wav"
    install_dir: str = ""
    """Legacy field kept only for older character rows/tests."""
    ref_audio_path: str = ""
    prompt_text: str = ""
    prompt_lang: str = "zh"
    text_lang: str = "zh"
    translate_target_lang: str = ""
    """Optional pre-TTS LLM translation target.

    When set (e.g. ``"ja"``), :class:`TTSService` first translates
    ``text`` from ``text_lang`` to this language via the translator
    port, then asks the external TTS service to synthesize the
    translated string using ``text_lang=<this>``. Lets a Japanese-voice
    character speak in their native voice even when the chat reply is
    Chinese.

    Empty (default) = no translation, synth source text as-is."""
    text_split_method: str = "cut5"
    """Legacy text splitting option kept for compatible TTS wrappers."""
    top_k: int = 5
    top_p: float = 1.0
    temperature: float = 1.0
    speed_factor: float = 1.0
    timeout_seconds: float = 90.0

    @property
    def enabled(self) -> bool:
        if self.provider == "openai":
            return bool(self.api_key)
        return bool(self.base_url)


@dataclass(frozen=True, slots=True)
class WebFetchSettings:
    """``web_fetch`` tool config.

    Always enabled (no API key needed — httpx + readability). The
    knobs are here so operators can tighten limits if a deployment
    is resource-constrained or widen them for long-form content.
    """

    timeout_seconds: float = 15.0
    max_html_bytes: int = 2_000_000
    max_text_chars: int = 6000


@dataclass(frozen=True, slots=True)
class TavilySearchSettings:
    """Tavily web-search tool config.

    Empty ``api_key`` means the ``web_search`` tool is not installed
    — the chat loop still works, it just won't have live browse
    capability. Tavily's free tier is plenty for a single-user
    deployment; key is per-account, obtained at tavily.com.
    """

    api_key: str = ""
    base_url: str = "https://api.tavily.com"
    max_results: int = 5
    """How many snippets to return per call. 5 fits under most context
    budgets while covering enough ground for concept lookups."""
    timeout_seconds: float = 15.0
    search_depth: str = "advanced"
    """``advanced`` pulls paragraph-level content + a synthesized
    answer, which is usually what the chat LLM needs to reason about
    novel concepts / recent events. ``basic`` returns shorter
    navigation-heavy snippets; only use it if Tavily credits are
    a concern."""

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True, slots=True)
class AnthropicProviderConfig:
    """Native Anthropic Claude provider config.

    Claude uses its own ``/v1/messages`` API (not OpenAI-compatible),
    with ``x-api-key`` + ``anthropic-version`` headers. Empty
    ``api_key`` means the provider isn't registered.
    """

    api_key: str = ""
    base_url: str = "https://api.anthropic.com"
    model: str = "claude-sonnet-4-5"
    anthropic_version: str = "2023-06-01"
    supports_vision: bool = True
    max_tokens: int = 4096
    """``/v1/messages`` *requires* ``max_tokens``. 4096 is a sane mid
    default — long enough for multi-paragraph replies, short enough
    that a runaway model doesn't eat a whole context window."""

    @property
    def enabled(self) -> bool:
        return bool(self.api_key)


@dataclass(frozen=True, slots=True)
class UserTimezoneSettings:
    """Default user timezone for civil-date interpretation.

    Server persistence remains UTC. This timezone is only for
    user-facing civil dates/times until per-user timezone persistence is
    introduced on ``operator_profiles``.
    """

    default_timezone_id: str = "UTC"

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "default_timezone_id",
            normalise_timezone_id(self.default_timezone_id),
        )


@dataclass(frozen=True, slots=True)
class CalendarSettings:
    """Real-world calendar facts (region holidays + 連假 detection).

    A pre-rendered natural-language block is injected into the schedule
    planner prompt and the chat prompt so the LLM can react to today's
    civil calendar (是學生就不該在端午節上課 / 上班族 blue Monday /
    連假最後一天的收心) without any hardcoded if-else.

    ``region`` is a country code recognised by the ``holidays`` PyPI
    package (``TW``, ``JP``, ``HK``, ``CN``, ``US``, ``KR``, …). Unknown
    codes degrade gracefully to a weekend-only calendar — the chat path
    keeps working, it just won't surface national-holiday context.

    ``enabled=False`` installs a null provider and the prompt blocks
    stay empty; deployments that don't want any holiday awareness can
    set ``KOKORO_CALENDAR_ENABLED=false``.
    """

    region: str = "TW"
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class WeatherSettings:
    """Real-world weather facts (current conditions + today's high/low).

    A pre-rendered natural-language block is injected into the chat,
    proactive, schedule-planner and feed-composer prompts so the LLM
    can naturally reflect real conditions ("外面在下雨改室內咖啡廳" /
    "好熱剛從外面回來") instead of hallucinating a generic 今天天氣很好.

    Backed by Open-Meteo (free, no API key needed). Operator configures
    a single lat/lon + label; the same fact block goes to every prompt
    site so character A's "下雨" and character B's "下雨" stay consistent.

    ``enabled=False`` or missing lat/lon installs a null provider and
    every prompt block stays empty — same fall-through shape as
    :class:`CalendarSettings`.
    """

    enabled: bool = True
    latitude: float | None = None
    longitude: float | None = None
    location_label: str = ""
    """Human-readable label that shows up in the prompt ("台北市")。
    Empty (the default) means "no deployment-wide label" — the weather
    provider is built with a label localized to ``default_primary_language``
    and each user's own location takes priority, so we never bake a raw
    Chinese literal into an en/ja deployment."""
    timezone_id: str = "auto"
    """IANA timezone passed to Open-Meteo's ``timezone`` parameter.
    ``"auto"`` lets the server infer from lat/lon — usually right."""
    cache_ttl_seconds: int = 15 * 60
    """Adapter-side TTL cache so chat-path doesn't hammer the API.
    15 minutes is a good balance between freshness and request volume."""


@dataclass(frozen=True, slots=True)
class GeoIpSettings:
    """IP-based location seed for new operator profiles."""

    enabled: bool = True
    provider: str = "ip-api"
    endpoint: str = "http://ip-api.com/json/"
    cache_ttl_seconds: int = 24 * 60 * 60
    timeout_seconds: float = 3.0


@dataclass(frozen=True, slots=True)
class NsfwModeSettings:
    """Temporary user-scoped NSFW mode settings."""

    ttl_seconds: int = 30 * 60


@dataclass(frozen=True, slots=True)
class WhatsAppSidecarSettings:
    """Container-side WhatsApp gateway connection defaults."""

    base_url: str = "http://whatsapp-sidecar:32190"
    api_token: str = ""


@dataclass(frozen=True, slots=True)
class WorldEventFeed:
    """Single RSS/Atom feed source for the world-event pool."""
    source_id: str
    url: str
    topic_tags: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class WorldEventSettings:
    """World-event pool configuration.

    feeds: registered RSS/Atom sources.
    retention_days: how long to keep events before pruning.
    scheduler_interval_seconds: how often the background task runs.
    """
    feeds: tuple[WorldEventFeed, ...] = field(default_factory=tuple)
    retention_days: int = 30
    scheduler_interval_seconds: float = 3600.0


@dataclass(frozen=True, slots=True)
class AutoConsolidationSettings:
    """Controls for the post-turn auto-consolidation trigger.

    Disabled by default on ``fake`` provider because the null consolidator
    can't actually merge anything, and enabling would just spin cycles.
    """

    enabled: bool = True
    threshold: int = 200
    cooldown_hours: float = 6.0


@dataclass(frozen=True, slots=True)
class PersonaSettings:
    """Operator-persona accumulation knobs.

    The persona system layers an ever-growing structured picture of the
    operator on top of the chat prompt. Two LLM jobs feed it:

    - Extraction (post-turn, background): scans the latest user message
      for facts and writes pending candidates.
    - Dream (proactive scheduler tick, quiet hours): consolidates
      pending → confirmed, supersedes outdated facts, decays stale ones.

    The numeric knobs below are statistical thresholds, *not* keyword
    rules — they decide when there's enough signal to act on, never
    *what* counts as a fact. Operators can tune them per deployment
    (more aggressive accumulation for power users, slower for new
    installs that want to keep the prompt minimal at first).

    ``familiarity_*`` thresholds drive the interaction-volume band rendered
    in Layer 4 prompts. They are statistical cutoffs over message counts +
    days-since-first-contact; raw counts never appear in the prompt itself,
    and initial relationship seeds remain the source of relationship truth.
    """

    enabled: bool = True
    """Master switch. ``False`` keeps the chat path working with the
    legacy OperatorProfile only — no extraction call, no dream tick,
    no persona block in prompts. Defaults on; flip via
    ``KOKORO_PERSONA_ENABLED=false`` if a deployment doesn't want the
    extra LLM cost."""

    curiosity_enabled: bool = True
    """Enable conversational persona-discovery planning on chat surfaces.

    This does not create a form or a hardcoded question loop. It only lets
    the LLM curiosity planner contribute a low-pressure writing hint when
    the existing persona aggregate is sparse. Flip via
    ``PERSONA_CURIOSITY_ENABLED=false`` or
    ``KOKORO_PERSONA_CURIOSITY_ENABLED=false``.
    """

    curiosity_proactive_enabled: bool = True
    """Enable persona curiosity as a candidate motive for proactive pushes.

    The proactive path still goes through first-message, quiet-hours,
    cooldown, daily-limit, intention-judge, and unanswered-streak prompts.
    Flip via ``PERSONA_CURIOSITY_PROACTIVE_ENABLED=false`` or
    ``KOKORO_PERSONA_CURIOSITY_PROACTIVE_ENABLED=false``.
    """

    extraction_in_background: bool = True
    """When ``True``, post-turn extraction is fire-and-forget through
    the chat service's background scheduler. Tests force ``False`` so
    they can await the result deterministically."""

    familiarity_stranger_max_msgs: int = 30
    familiarity_stranger_max_days: int = 2
    familiarity_acquaintance_max_msgs: int = 200
    familiarity_acquaintance_max_days: int = 14
    familiarity_close_min_days: int = 30

    recent_activity_frequent_min_7d: int = 50
    """≥ this many user messages in the last 7 days → render Layer 4 as
    "最近聊得很頻繁"."""
    recent_activity_inactive_max_7d: int = 5
    """≤ this many user messages in the last 7 days → render Layer 4 as
    "最近久未聯絡"; in between → "最近偶爾聊"."""

    dream_quiet_hours_start: int = 2
    dream_quiet_hours_end: int = 6
    """Operator-timezone hour bounds for the dream pass.

    HUMANIZATION_ROADMAP §4.5 (2026-05-21 owner decision) pinned the
    default to 02:00–06:00 — see ``QuietHoursService`` which now reads
    from ``app_runtime_settings`` first and falls back to these env
    defaults. ``start > end`` represents a window that wraps midnight.
    """
    dream_min_pending: int = 5
    dream_min_interval_hours: int = 6

    decay_after_days: int = 60
    """Confirmed fields older than this start losing confidence each
    dream pass."""
    stale_after_days: int = 180
    """Past this age a field is hidden from the prompt (state=stale)."""

    interaction_strength_cache_ttl_seconds: float = 60.0


@dataclass(frozen=True, slots=True)
class HumanizationSettings:
    """Feature flags for the P1 擬人化 backlog (`docs/HUMANIZATION_ROADMAP.md`).

    Every dream-time / proactive-time LLM job introduced under §3 mounts
    behind one of these flags. All default ``True`` so a fresh install
    gets the full humanization stack, but operators can flip any one off
    when model rotation, quiet-hours budget, or experiment isolation
    demands it. Per §5 (2026-05-21 decision) of the roadmap.

    Route B (cross-character hearsay, §4.3) is intentionally **off** by
    default — that branch reshapes per-character isolation, so opting in
    must be explicit.
    """

    relationship_milestone_enabled: bool = True
    """§3.5 — let the dream pass append ``relationship_milestone`` memory
    when the Familiarity band crosses a threshold."""

    disposition_drift_enabled: bool = True
    """§3.1 — dream-time ``DispositionDriftService`` nudges
    ``CharacterDisposition`` bands based on long-window evidence."""

    self_reflection_enabled: bool = True
    """§3.2 — dream-time self-narrative reflection over 7/30 day windows."""

    behavioral_pattern_enabled: bool = True
    """§3.3 — recurring-pattern observer (schedule routine + verbal habits)."""

    deferred_intent_enabled: bool = True
    """§3.4 — keep intention-judge rejected proactive motives in a 24h
    TTL queue and re-evaluate next tick."""

    route_b_enabled: bool = False
    """§4.3 — Route B cross-character encounter dialogue. Off by default
    per §5 2026-05-21 decision (P2 surface, opens after P1 stable)."""

    body_state_enabled: bool = True
    """§4.1 — embodied signal injection (hunger / thirst / sleep_debt /
    seasonal_allergy). All-low ``BodyState`` already collapses the
    prompt block to empty, but this flag also lets operators force-skip
    when comparing prompt variants."""

    subjective_time_enabled: bool = True
    """§4.4 — topical-layer 久未聯絡 catch-up hint. Disable to fall back
    to the older "raw idle minutes only" prompt shape for A/B work."""

    address_preference_enabled: bool = True
    """§4.2 — post-turn ``OperatorAddressPreference`` extractor + prompt
    injection. Disable to suppress auto-observed register hints when
    diagnosing whether the LLM is mirroring user style too aggressively."""

    relationship_coherence_enabled: bool = True
    """Dream-time relationship-coherence self-heal. When on, the dream
    pass tail runs a high-reasoning detector that repairs address/identity
    contamination (direction inversions) in persona name/nickname, the
    observed salutation, and recent memory participant attribution.
    Best-effort and fail-soft; disable to isolate the dream pass when
    diagnosing self-heal behaviour."""

    relationship_coherence_transcript_window: int = 24
    """How many recent raw dialogue turns feed the coherence detector as
    first-hand evidence. Bounds LLM cost; 0 disables transcript context
    (the detector then relies on seed / rename-log authority only)."""

    @classmethod
    def from_env(cls) -> "HumanizationSettings":
        return cls(
            relationship_milestone_enabled=_parse_bool(
                os.getenv("KOKORO_HUMANIZATION_RELATIONSHIP_MILESTONE_ENABLED"),
                default=True,
            ),
            disposition_drift_enabled=_parse_bool(
                os.getenv("KOKORO_HUMANIZATION_DISPOSITION_DRIFT_ENABLED"),
                default=True,
            ),
            self_reflection_enabled=_parse_bool(
                os.getenv("KOKORO_HUMANIZATION_SELF_REFLECTION_ENABLED"),
                default=True,
            ),
            behavioral_pattern_enabled=_parse_bool(
                os.getenv("KOKORO_HUMANIZATION_BEHAVIORAL_PATTERN_ENABLED"),
                default=True,
            ),
            deferred_intent_enabled=_parse_bool(
                os.getenv("KOKORO_HUMANIZATION_DEFERRED_INTENT_ENABLED"),
                default=True,
            ),
            route_b_enabled=_parse_bool(
                os.getenv("KOKORO_HUMANIZATION_ROUTE_B_ENABLED"),
                default=False,
            ),
            body_state_enabled=_parse_bool(
                os.getenv("KOKORO_HUMANIZATION_BODY_STATE_ENABLED"),
                default=True,
            ),
            subjective_time_enabled=_parse_bool(
                os.getenv("KOKORO_HUMANIZATION_SUBJECTIVE_TIME_ENABLED"),
                default=True,
            ),
            address_preference_enabled=_parse_bool(
                os.getenv("KOKORO_HUMANIZATION_ADDRESS_PREFERENCE_ENABLED"),
                default=True,
            ),
            relationship_coherence_enabled=_parse_bool(
                os.getenv("KOKORO_HUMANIZATION_RELATIONSHIP_COHERENCE_ENABLED"),
                default=True,
            ),
            relationship_coherence_transcript_window=max(
                0,
                _parse_int(
                    os.getenv(
                        "KOKORO_HUMANIZATION_RELATIONSHIP_COHERENCE_"
                        "TRANSCRIPT_WINDOW",
                    ),
                    default=24,
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class PromptQualitySettings:
    """Prompt quality gates for player-visible replies.

    Material digest and reply quality gate are default-on. Chat keeps
    incremental streaming on low-risk turns through the risk gate; high-risk
    turns, proactive messages, and feed posts can spend the extra review call.
    """

    material_digest_enabled: bool = True
    novelty_gate_enabled: bool = True
    novelty_gate_max_retries: int = 1
    register_profile_enabled: bool = True
    reply_quality_gate_risk_threshold: float = 0.65
    reply_quality_similarity_threshold: float = 0.88

    @classmethod
    def from_env(cls) -> "PromptQualitySettings":
        return cls(
            material_digest_enabled=_parse_bool(
                os.getenv("KOKORO_PROMPT_MATERIAL_DIGEST_ENABLED"),
                default=True,
            ),
            novelty_gate_enabled=_parse_bool(
                os.getenv("KOKORO_NOVELTY_GATE_ENABLED"),
                default=True,
            ),
            novelty_gate_max_retries=max(
                0,
                _parse_int(
                    os.getenv("KOKORO_NOVELTY_GATE_MAX_RETRIES"),
                    default=1,
                ),
            ),
            register_profile_enabled=_parse_bool(
                os.getenv("KOKORO_REGISTER_PROFILE_ENABLED"),
                default=True,
            ),
            reply_quality_gate_risk_threshold=max(
                0.0,
                min(
                    1.0,
                    _parse_float(
                        os.getenv("KOKORO_REPLY_QUALITY_GATE_RISK_THRESHOLD"),
                        default=0.65,
                    ),
                ),
            ),
            reply_quality_similarity_threshold=max(
                0.0,
                min(
                    1.0,
                    _parse_float(
                        os.getenv("KOKORO_REPLY_QUALITY_SIMILARITY_THRESHOLD"),
                        default=0.88,
                    ),
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class MemoirSettings:
    """Player-side memoir thresholds and limits (docs/MEMOIR_PLAN.md).

    All defaults are conservative and tuned for the MVP — they're exposed
    as env vars so operators can adjust without redeploying once real
    distribution data is available.
    """

    memory_min_salience: float = 0.7
    """Minimum ``MemoryItem.salience`` to surface in the memoir timeline.
    ``RELATIONSHIP_MILESTONE`` rows already carry fixed-high salience so
    they pass this threshold by construction."""
    emotion_min_intensity: float = 0.65
    """Minimum ``EmotionEvent.intensity`` to surface as a timeline entry."""
    emotion_lookback_days: int = 90
    """How far back to scan ``EmotionEvent`` rows. Older events have
    decayed in influence and rarely read as "memorable highlights"."""
    timeline_limit: int = 80
    """Hard cap on timeline entry count per request — pins always survive
    the cut so heavy users can still see their pinned set."""
    pin_max_per_pair: int = 32
    """Maximum pin count per (character, operator). Exceeding returns
    HTTP 409 — the UI surfaces this so the player must un-pin before
    pinning more. Deliberately *not* a FIFO rolling cap; pinning is
    framed as a precious-choice gesture."""

    @classmethod
    def from_env(cls) -> "MemoirSettings":
        return cls(
            memory_min_salience=_parse_float(
                os.getenv("KOKORO_MEMOIR_MEMORY_MIN_SALIENCE"),
                default=0.7,
            ),
            emotion_min_intensity=_parse_float(
                os.getenv("KOKORO_MEMOIR_EMOTION_MIN_INTENSITY"),
                default=0.65,
            ),
            emotion_lookback_days=_parse_int(
                os.getenv("KOKORO_MEMOIR_EMOTION_LOOKBACK_DAYS"),
                default=90,
            ),
            timeline_limit=_parse_int(
                os.getenv("KOKORO_MEMOIR_TIMELINE_LIMIT"),
                default=80,
            ),
            pin_max_per_pair=_parse_int(
                os.getenv("KOKORO_MEMOIR_PIN_MAX_PER_PAIR"),
                default=32,
            ),
        )


@dataclass(frozen=True, slots=True)
class AuthSettings:
    """Multi-user auth config (MULTI_USER_AUTH_PLAN).

    ``enabled=False`` (default) skips the auth dependency and runs
    every request as the singleton ``DEFAULT_OPERATOR_ID`` user. The
    other fields are unused in that mode; we still parse them so a
    deployment that flips ``enabled`` mid-life doesn't restart with a
    silently empty secret.

    ``jwt_secret`` empty + ``enabled=True`` causes container build
    failure — there's no safe default; refusing to start is better
    than issuing tokens an attacker can forge.

    ``bootstrap_admin_email`` / ``bootstrap_admin_password`` are an
    optional first-run automation: when both are set and the default
    user still has no credentials, the lifespan startup hook attaches
    them via ``AuthService.setup_initial_admin``. After the first
    successful run the env vars become no-ops (the seed only fires
    while ``needs_setup`` is True). Empty values disable the hook
    entirely — the operator can still go through ``/auth/setup`` by
    hand. Independent of ``enabled`` so a deployment can pre-seed
    before flipping the auth switch.
    """

    enabled: bool = False
    jwt_secret: str = ""
    jwt_ttl_seconds: int = 7 * 24 * 60 * 60
    bootstrap_admin_email: str = ""
    bootstrap_admin_password: str = ""

    @classmethod
    def from_env(cls) -> "AuthSettings":
        enabled = _parse_bool(
            os.getenv("AUTH_ENABLED", os.getenv("KOKORO_AUTH_ENABLED")),
            default=False,
        )
        secret = (
            os.getenv("JWT_SECRET", "")
            or os.getenv("KOKORO_JWT_SECRET", "")
            or ""
        ).strip()
        ttl = max(60, _parse_int(
            os.getenv("JWT_TTL_SECONDS", os.getenv("KOKORO_JWT_TTL_SECONDS")),
            default=7 * 24 * 60 * 60,
        ))
        # Bootstrap admin credentials are sensitive — strip whitespace
        # but do not lowercase here; ``AuthService._normalise_email``
        # handles canonicalisation at the service boundary.
        bootstrap_email = (
            os.getenv("BOOTSTRAP_ADMIN_EMAIL", "")
            or os.getenv("KOKORO_BOOTSTRAP_ADMIN_EMAIL", "")
        ).strip()
        bootstrap_password = (
            os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
            or os.getenv("KOKORO_BOOTSTRAP_ADMIN_PASSWORD", "")
        )
        return cls(
            enabled=enabled,
            jwt_secret=secret,
            jwt_ttl_seconds=ttl,
            bootstrap_admin_email=bootstrap_email,
            bootstrap_admin_password=bootstrap_password,
        )


@dataclass(frozen=True, slots=True)
class CloudSettings:
    """Yuralume Cloud hosted-core integration switch.

    Phase A only exposes mode/configuration and locks self-host-only
    local auth/provider surfaces. Later phases consume the URLs/token
    for federated login and resource routing.
    """

    enabled: bool = False
    user_service_url: str = ""
    gateway_url: str = ""
    deployment_token: str = ""
    deployment_id: str = "hosted-primary"
    deployment_audience: str = "yuralume-gateway"
    # Shared secret for the User service internal hosted-play code exchange
    # (X-Internal-Token). Blank in demo-only deployments; the SSO entry flow
    # (plan H0) requires it to be set on both Core and the User service.
    hosted_play_internal_token: str = ""
    # Versioned caller/audience/scope credential for User internal APIs.
    internal_service_credential: str = ""
    introspect_timeout: float = 5.0
    session_ttl_seconds: int = 3600
    llm_model_presets: dict[str, str] = field(default_factory=dict)
    image_preset: str = "yuralume-anime"
    video_preset: str = "yuralume-anime"
    tts_voice_default: str = ""
    # When true (cloud mode), resolve feature_key -> preset from the control-plane
    # routing profile instead of the env preset map (which becomes a deprecated
    # one-release fallback). See docs/CLOUD_CONTROL_PLANE_CONFIG_PLAN.md Phase 3.
    runtime_config_enabled: bool = False
    # Optional shared secret sent as ``X-Internal-Token`` on Core's
    # runtime-config pulls (core-profile + per-tier runtime-profile). When set
    # it lets the hosted User service authenticate the internal channel instead
    # of leaving it open. Blank = no header (self-host / backward-compat).
    runtime_config_internal_token: str = ""
    # Hosted deployments set this True to reject free ``standard`` tenants at
    # Core login (H1). Default False keeps self-host / existing cloud behavior
    # where any active account may log in. ``demo`` is always allowed.
    require_paid_tier: bool = False

    @property
    def active(self) -> bool:
        return self.enabled


@dataclass(frozen=True, slots=True)
class DemoOAuthSettings:
    """Public demo OAuth client ids served to the SPA at runtime (plan Phase 5.1).

    These are PUBLIC client ids, not secrets. Serving them at runtime removes the
    Vite build-time bake (``VITE_YURALUME_DEMO_*_CLIENT_ID``) so changing a client
    id no longer requires rebuilding the SPA/GHCR image. Provider *secrets* stay
    server-side in the Cloud User service.
    """

    discord_client_id: str = ""
    google_client_id: str = ""

    @classmethod
    def from_env(cls) -> "DemoOAuthSettings":
        return cls(
            discord_client_id=os.getenv(
                "YURALUME_DEMO_DISCORD_CLIENT_ID", "",
            ).strip(),
            google_client_id=os.getenv(
                "YURALUME_DEMO_GOOGLE_CLIENT_ID", "",
            ).strip(),
        )

    def client_id_for(self, provider: str) -> str:
        normalized = (provider or "").strip().lower()
        if normalized == "discord":
            return self.discord_client_id
        if normalized == "google":
            return self.google_client_id
        return ""


@dataclass(frozen=True, slots=True)
class WebPushSettings:
    vapid_public_key: str = ""
    vapid_private_key: str = ""
    vapid_subject: str = "mailto:admin@example.invalid"
    # How long the push service should retain an undeliverable message
    # for store-and-forward. TTL=0 (pywebpush default) makes pushes
    # live-only: a message is dropped the instant the device is offline
    # / dozing instead of being delivered when it reconnects. Default 24h.
    ttl_seconds: int = 86400

    @property
    def configured(self) -> bool:
        return bool(self.vapid_public_key and self.vapid_private_key)


@dataclass(frozen=True, slots=True)
class AppSettings:
    default_provider_id: str = "fake"
    database_url: str = ""
    deployment_mode: str = "container"
    auth: AuthSettings = field(default_factory=AuthSettings)
    cloud: CloudSettings = field(default_factory=CloudSettings)
    demo_oauth: DemoOAuthSettings = field(default_factory=DemoOAuthSettings)
    web_push: WebPushSettings = field(default_factory=WebPushSettings)
    config_encryption_key: str = ""
    openai_compatible_providers: tuple[dict[str, str | None], ...] = field(default_factory=tuple)
    embedding: EmbeddingSettings = field(default_factory=EmbeddingSettings)
    storage: ObjectStorageSettings = field(default_factory=ObjectStorageSettings)
    image_api: MediaApiSettings = field(default_factory=MediaApiSettings)
    video_api: MediaApiSettings = field(default_factory=MediaApiSettings)
    comfyui: ComfyUISettings = field(default_factory=ComfyUISettings)
    openai_image: OpenAIImageSettings = field(default_factory=OpenAIImageSettings)
    image_provider: str = "comfyui"
    """Legacy single-provider switch kept only for old tests/config."""
    image_profiles_raw: str = ""
    """Raw operator config for named image profiles — either an inline
    JSON list or a path to a JSON file. Set via
    ``KOKORO_IMAGE_PROFILES``. When empty, the container may synthesise
    a simple ``external_api`` profile from ``KOKORO_IMAGE_API_*``."""
    video_profiles_raw: str = ""
    """Raw operator config for named video profiles.
    Set via ``KOKORO_VIDEO_PROFILES``. When empty, the container may
    synthesise a simple ``external_api`` profile from ``KOKORO_VIDEO_API_*``."""
    tts: TTSSettings = field(default_factory=TTSSettings)
    tavily: TavilySearchSettings = field(default_factory=TavilySearchSettings)
    web_fetch: WebFetchSettings = field(default_factory=WebFetchSettings)
    auto_consolidation: AutoConsolidationSettings = field(
        default_factory=AutoConsolidationSettings,
    )
    world_events: WorldEventSettings = field(default_factory=WorldEventSettings)
    anthropic: AnthropicProviderConfig = field(default_factory=AnthropicProviderConfig)
    user_timezone: UserTimezoneSettings = field(default_factory=UserTimezoneSettings)
    default_primary_language: str = DEFAULT_PRIMARY_LANGUAGE
    """Deploy-time default interface + content language (BCP 47).

    Read from ``USER_PRIMARY_LANGUAGE`` (parallel to ``USER_TIMEZONE``).
    In single-user mode it seeds the unconfigured ``default`` operator at
    boot so both the UI chrome and the LLM content language come up in the
    operator's language without anyone touching the in-app switcher. In
    multi-user mode each operator instead pins this at ``/auth/setup``."""
    calendar: CalendarSettings = field(default_factory=CalendarSettings)
    weather: WeatherSettings = field(default_factory=WeatherSettings)
    geoip: GeoIpSettings = field(default_factory=GeoIpSettings)
    nsfw_mode: NsfwModeSettings = field(default_factory=NsfwModeSettings)
    whatsapp_sidecar: WhatsAppSidecarSettings = field(
        default_factory=WhatsAppSidecarSettings,
    )
    persona: PersonaSettings = field(default_factory=PersonaSettings)
    humanization: HumanizationSettings = field(default_factory=HumanizationSettings)
    prompt_quality: PromptQualitySettings = field(default_factory=PromptQualitySettings)
    memoir: MemoirSettings = field(default_factory=MemoirSettings)
    public_base_url: str = ""
    """Externally-reachable URL of this backend (no trailing slash).

    Set via ``APP_BASE_URL`` / ``KOKORO_PUBLIC_BASE_URL``. Used to promote relative
    ``/uploads/...`` URLs to absolute when handing attachments off to
    external platforms (Telegram sendPhoto, LINE image message) and as
    the canonical media host when storage-local would otherwise expose
    loopback URLs. Empty disables the rewrite — fine for local-only dev,
    but TG/LINE image delivery won't work until this is set.
    """
    uploads_dir: Path = field(default_factory=lambda: Path("uploads"))
    """Filesystem root for character image uploads.

    Served by the FastAPI app at ``/uploads/*``. Relative paths are
    resolved against the repo root at startup. We store *relative* URLs
    in the DB (``/uploads/characters/<id>/<file>``) so the data is
    portable across hosts.
    """
    debug_ui_enabled: bool = False
    """Surface developer-facing admin panels (observability, experiments,
    pending follow-ups, subsystem health metrics, persona disposition drift /
    behavioural patterns) in the frontend.

    Driven by ``KOKORO_DEBUG_UI_ENABLED``. Default is ``False`` so the
    public self-host build hides the panels and gives players a clean
    surface; flipping the env to ``true`` re-exposes them for the
    deployment owner's own debugging. The backend admin APIs are
    *always* available — this flag only controls rendering, so the
    owner can still `curl` for telemetry exports either way."""

    @property
    def use_database(self) -> bool:
        return bool(self.database_url)

    @property
    def use_embedder(self) -> bool:
        return bool(self.embedding.model and self.embedding.base_url)

    @classmethod
    def from_env(cls, project_root: Path | None = None) -> "AppSettings":
        root = project_root or Path(__file__).resolve().parents[3]
        load_dotenv(root / ".env", override=False)

        providers: list[dict[str, str | None]] = []
        for provider_id, prefix, default_base_url, default_model in (
            _OPENAI_COMPATIBLE_CLOUD_PROVIDERS
        ):
            api_key = os.getenv(f"KOKORO_{prefix}_API_KEY", "").strip()
            if not api_key:
                continue
            providers.append(
                {
                    "provider_id": provider_id,
                    "base_url": os.getenv(
                        f"KOKORO_{prefix}_BASE_URL", default_base_url,
                    ).rstrip("/"),
                    "api_key": api_key,
                    "model": os.getenv(
                        f"KOKORO_{prefix}_MODEL", default_model,
                    ),
                    "supports_vision": _parse_bool(
                        os.getenv(f"KOKORO_{prefix}_SUPPORTS_VISION"),
                        # Cloud providers in this list all support vision
                        # on their flagship models. Operators who pick a
                        # text-only variant can flip the env var off.
                        default=True,
                    ),
                }
            )

        lmstudio_model = os.getenv("KOKORO_LMSTUDIO_MODEL")
        if lmstudio_model:
            # Vision capability can't be reliably sniffed at runtime for
            # self-hosted endpoints — operator flips a flag to opt in.
            supports_vision_raw = os.getenv(
                "KOKORO_LMSTUDIO_SUPPORTS_VISION", "false",
            ).strip().lower()
            supports_vision = supports_vision_raw in {"1", "true", "yes", "on"}
            # LM Studio's default max_tokens is a server-side surprise
            # (~512 tokens on several builds) which silently truncates
            # long tool-call JSON mid-argument. Default to 4096 here so
            # the common "forced /pic → generate_image" path can fit a
            # full danbooru tag list + caption without getting chopped.
            lmstudio_max_tokens = _parse_int(
                os.getenv("KOKORO_LMSTUDIO_MAX_TOKENS"), default=4096,
            )
            providers.append(
                {
                    "provider_id": "lmstudio",
                    "base_url": os.getenv("KOKORO_LMSTUDIO_BASE_URL", "http://127.0.0.1:1234/v1"),
                    "api_key": os.getenv("KOKORO_LMSTUDIO_API_KEY", "lm-studio"),
                    "model": lmstudio_model,
                    "supports_vision": supports_vision,
                    "max_tokens": max(64, lmstudio_max_tokens),
                }
            )

        anthropic_key = os.getenv("KOKORO_ANTHROPIC_API_KEY", "").strip()
        anthropic = AnthropicProviderConfig(
            api_key=anthropic_key,
            base_url=os.getenv(
                "KOKORO_ANTHROPIC_BASE_URL", "https://api.anthropic.com",
            ).rstrip("/"),
            model=os.getenv(
                "KOKORO_ANTHROPIC_MODEL", "claude-sonnet-4-5",
            ),
            anthropic_version=os.getenv(
                "KOKORO_ANTHROPIC_VERSION", "2023-06-01",
            ),
            supports_vision=_parse_bool(
                os.getenv("KOKORO_ANTHROPIC_SUPPORTS_VISION"),
                default=True,
            ),
            max_tokens=max(
                64,
                _parse_int(os.getenv("KOKORO_ANTHROPIC_MAX_TOKENS"), default=4096),
            ),
        )

        fallback_default = "fake"
        if anthropic.enabled:
            fallback_default = "anthropic"
        elif providers:
            fallback_default = str(providers[0]["provider_id"])
        default_provider_id = os.getenv(
            "KOKORO_DEFAULT_PROVIDER_ID", fallback_default,
        )
        database_url = os.getenv("DATABASE_URL", os.getenv("KOKORO_DATABASE_URL", ""))

        # Embedding model defaults to reusing the LM Studio base URL / key
        # so the common "one local server, several models loaded" setup
        # works with zero extra config. Override explicitly when the
        # embedder lives somewhere else.
        embedding_model = os.getenv(
            "EMBEDDING_MODEL",
            os.getenv("KOKORO_EMBEDDING_MODEL", ""),
        )
        embedding_base_url = os.getenv(
            "EMBEDDING_BASE_URL",
            os.getenv(
                "KOKORO_EMBEDDING_BASE_URL",
                os.getenv("KOKORO_LMSTUDIO_BASE_URL", ""),
            ),
        )
        embedding_api_key = os.getenv(
            "EMBEDDING_API_KEY",
            os.getenv(
                "KOKORO_EMBEDDING_API_KEY",
                os.getenv("KOKORO_LMSTUDIO_API_KEY"),
            ),
        )
        embedding_dimension_raw = os.getenv(
            "EMBEDDING_DIMENSION",
            os.getenv("KOKORO_EMBEDDING_DIMENSION", "1024"),
        )
        try:
            embedding_dimension = int(embedding_dimension_raw)
        except ValueError:
            embedding_dimension = 1024
        embedding = EmbeddingSettings(
            model=embedding_model,
            base_url=embedding_base_url,
            api_key=embedding_api_key,
            dimension=embedding_dimension,
        )

        web_fetch = WebFetchSettings(
            timeout_seconds=max(
                1.0,
                _parse_float(
                    os.getenv(
                        "WEB_FETCH_TIMEOUT",
                        os.getenv("KOKORO_WEB_FETCH_TIMEOUT"),
                    ),
                    default=15.0,
                ),
            ),
            max_html_bytes=max(
                10_000,
                _parse_int(
                    os.getenv(
                        "WEB_FETCH_MAX_HTML_BYTES",
                        os.getenv("KOKORO_WEB_FETCH_MAX_HTML_BYTES"),
                    ),
                    default=2_000_000,
                ),
            ),
            max_text_chars=max(
                500,
                _parse_int(
                    os.getenv(
                        "WEB_FETCH_MAX_TEXT_CHARS",
                        os.getenv("KOKORO_WEB_FETCH_MAX_TEXT_CHARS"),
                    ),
                    default=6000,
                ),
            ),
        )

        tavily = TavilySearchSettings(
            api_key=(
                os.getenv("TAVILY_API_KEY", "")
                or os.getenv("KOKORO_TAVILY_API_KEY", "")
            ).strip(),
            base_url=os.getenv(
                "TAVILY_BASE_URL",
                os.getenv("KOKORO_TAVILY_BASE_URL", "https://api.tavily.com"),
            ).rstrip("/"),
            max_results=max(
                1,
                _parse_int(
                    os.getenv(
                        "TAVILY_MAX_RESULTS",
                        os.getenv("KOKORO_TAVILY_MAX_RESULTS"),
                    ),
                    default=5,
                ),
            ),
            timeout_seconds=max(
                1.0,
                _parse_float(
                    os.getenv("TAVILY_TIMEOUT", os.getenv("KOKORO_TAVILY_TIMEOUT")),
                    default=15.0,
                ),
            ),
            search_depth=(
                os.getenv(
                    "TAVILY_SEARCH_DEPTH",
                    os.getenv("KOKORO_TAVILY_SEARCH_DEPTH", "advanced"),
                ).strip()
                or "advanced"
            ),
        )

        comfyui = ComfyUISettings(
            server=os.getenv("KOKORO_COMFYUI_SERVER", ""),
            checkpoint=os.getenv(
                "KOKORO_COMFYUI_CHECKPOINT",
                "waiNSFWIllustrious_v140.safetensors",
            ),
            workflow_file=os.getenv("KOKORO_COMFYUI_WORKFLOW_FILE", ""),
            generation_timeout_seconds=_parse_float(
                os.getenv("KOKORO_COMFYUI_TIMEOUT"), default=180.0,
            ),
            lora_dir=os.getenv("KOKORO_COMFYUI_LORA_DIR", ""),
        )

        # API key + base URL: prefer the image-specific env, fall back to
        # the shared OpenAI env (``KOKORO_OPENAI_API_KEY`` /
        # ``KOKORO_OPENAI_BASE_URL``) that's already used by the LLM
        # provider. Lets operators wire OpenAI once and have both LLM and
        # GPT Image 2 pick up the same credential — the override exists
        # only for the rare case of needing distinct keys / endpoints
        # for the two surfaces.
        openai_image_api_key = (
            os.getenv("KOKORO_OPENAI_IMAGE_API_KEY", "").strip()
            or os.getenv("KOKORO_OPENAI_API_KEY", "").strip()
        )
        openai_image_base_url = (
            os.getenv("KOKORO_OPENAI_IMAGE_BASE_URL", "").strip()
            or os.getenv("KOKORO_OPENAI_BASE_URL", "").strip()
            or "https://api.openai.com/v1"
        )
        openai_image = OpenAIImageSettings(
            api_key=openai_image_api_key,
            model=os.getenv(
                "KOKORO_OPENAI_IMAGE_MODEL", "gpt-image-2",
            ).strip() or "gpt-image-2",
            quality=os.getenv(
                "KOKORO_OPENAI_IMAGE_QUALITY", "medium",
            ).strip() or "medium",
            timeout_seconds=max(
                5.0,
                _parse_float(
                    os.getenv("KOKORO_OPENAI_IMAGE_TIMEOUT"), default=180.0,
                ),
            ),
            base_url=openai_image_base_url.rstrip("/"),
        )
        image_provider_raw = os.getenv(
            "KOKORO_IMAGE_PROVIDER", "comfyui",
        ).strip().lower() or "comfyui"
        image_provider = (
            image_provider_raw
            if image_provider_raw in {"comfyui", "openai"}
            else "comfyui"
        )
        image_profiles_raw = os.getenv("KOKORO_IMAGE_PROFILES", "").strip()
        video_profiles_raw = os.getenv("KOKORO_VIDEO_PROFILES", "").strip()
        image_api_key = os.getenv("KOKORO_IMAGE_API_KEY", "").strip()
        image_api_provider = (
            os.getenv("KOKORO_IMAGE_API_PROVIDER", "gateway").strip().lower()
            or "gateway"
        )
        image_api = MediaApiSettings(
            base_url=_media_api_base_url(
                explicit=os.getenv("KOKORO_IMAGE_API_BASE_URL", "").strip(),
                provider=image_api_provider,
                media_kind="image",
            ),
            api_key=image_api_key,
            model=os.getenv("KOKORO_IMAGE_API_MODEL", "").strip()
            or _media_api_default_model(
                provider=image_api_provider,
                media_kind="image",
            ),
            provider=image_api_provider,
            timeout_seconds=max(
                5.0,
                _parse_float(
                    os.getenv("KOKORO_IMAGE_API_TIMEOUT"), default=180.0,
                ),
            ),
        )
        video_api_provider = (
            os.getenv("KOKORO_VIDEO_API_PROVIDER", "gateway").strip().lower()
            or "gateway"
        )
        video_api = MediaApiSettings(
            base_url=_media_api_base_url(
                explicit=os.getenv("KOKORO_VIDEO_API_BASE_URL", "").strip(),
                provider=video_api_provider,
                media_kind="video",
            ),
            api_key=os.getenv("KOKORO_VIDEO_API_KEY", "").strip(),
            model=os.getenv("KOKORO_VIDEO_API_MODEL", "").strip()
            or _media_api_default_model(
                provider=video_api_provider,
                media_kind="video",
            ),
            provider=video_api_provider,
            timeout_seconds=max(
                30.0,
                _parse_float(
                    os.getenv("KOKORO_VIDEO_API_TIMEOUT"), default=1800.0,
                ),
            ),
        )

        tts = TTSSettings(
            provider=os.getenv("KOKORO_TTS_PROVIDER", "api").strip().lower()
            or "api",
            base_url=os.getenv("KOKORO_TTS_BASE_URL", "").rstrip("/"),
            api_key=os.getenv("KOKORO_TTS_API_KEY", "").strip(),
            model=os.getenv("KOKORO_TTS_MODEL", "").strip(),
            voice_id=os.getenv("KOKORO_TTS_VOICE_ID", "").strip(),
            response_format=(
                os.getenv("KOKORO_TTS_RESPONSE_FORMAT", "wav").strip() or "wav"
            ),
            install_dir=os.getenv("KOKORO_TTS_INSTALL_DIR", "").strip(),
            ref_audio_path=os.getenv("KOKORO_TTS_REF_AUDIO_PATH", "").strip(),
            prompt_text=os.getenv("KOKORO_TTS_PROMPT_TEXT", "").strip(),
            prompt_lang=os.getenv("KOKORO_TTS_PROMPT_LANG", "zh").strip() or "zh",
            text_lang=os.getenv("KOKORO_TTS_TEXT_LANG", "zh").strip() or "zh",
            translate_target_lang=os.getenv(
                "KOKORO_TTS_TRANSLATE_TARGET_LANG", "",
            ).strip(),
            text_split_method=(
                os.getenv("KOKORO_TTS_TEXT_SPLIT_METHOD", "cut5").strip() or "cut5"
            ),
            top_k=_parse_int(os.getenv("KOKORO_TTS_TOP_K"), default=5),
            top_p=_parse_float(os.getenv("KOKORO_TTS_TOP_P"), default=1.0),
            temperature=_parse_float(
                os.getenv("KOKORO_TTS_TEMPERATURE"), default=1.0,
            ),
            speed_factor=_parse_float(
                os.getenv("KOKORO_TTS_SPEED"), default=1.0,
            ),
            timeout_seconds=max(
                5.0,
                _parse_float(os.getenv("KOKORO_TTS_TIMEOUT"), default=90.0),
            ),
        )

        auto_consolidation = _load_auto_consolidation_settings(default_provider_id)
        world_events = _load_world_event_settings()
        persona = _load_persona_settings()
        calendar = CalendarSettings(
            region=(
                os.getenv(
                    "CALENDAR_REGION",
                    os.getenv("KOKORO_CALENDAR_REGION", "TW"),
                ).strip()
                or "TW"
            ).upper(),
            enabled=_parse_bool(
                os.getenv(
                    "CALENDAR_ENABLED",
                    os.getenv("KOKORO_CALENDAR_ENABLED"),
                ),
                default=True,
            ),
        )
        weather = _load_weather_settings()
        geoip = _load_geoip_settings()
        whatsapp_sidecar = _load_whatsapp_sidecar_settings()
        user_timezone = _load_user_timezone_settings()
        default_primary_language = _load_default_primary_language()

        uploads_raw = os.getenv("KOKORO_UPLOADS_DIR", "uploads")
        uploads_dir = Path(uploads_raw)
        if not uploads_dir.is_absolute():
            uploads_dir = (root / uploads_raw).resolve()
        public_base_url = (
            os.getenv("APP_BASE_URL", "")
            or os.getenv("PUBLIC_BASE_URL", "")
            or os.getenv("KOKORO_PUBLIC_BASE_URL", "")
        ).rstrip("/")
        configured_storage_public_base_url = (
            os.getenv("STORAGE_PUBLIC_URL", "")
            or os.getenv("STORAGE_PUBLIC_BASE_URL", "")
            or os.getenv("KOKORO_STORAGE_PUBLIC_BASE_URL", "")
        ).rstrip("/")
        deployment_mode = (
            os.getenv(
                "DEPLOYMENT_MODE",
                os.getenv("KOKORO_DEPLOYMENT_MODE", "container"),
            ).strip().lower()
            or "container"
        )
        storage = ObjectStorageSettings(
            provider=(
                os.getenv(
                    "STORAGE_PROVIDER",
                    os.getenv("KOKORO_STORAGE_PROVIDER", "http"),
                ).strip().lower()
                or "http"
            ),
            base_url=(
                os.getenv("STORAGE_URL", "")
                or os.getenv("STORAGE_BASE_URL", "")
                or os.getenv("KOKORO_STORAGE_BASE_URL", "")
            ).rstrip("/"),
            api_key=(
                os.getenv("STORAGE_KEY", "")
                or os.getenv("STORAGE_API_KEY", "")
                or os.getenv("KOKORO_STORAGE_API_KEY", "")
            ).strip(),
            public_base_url=_resolve_storage_public_base_url(
                configured=configured_storage_public_base_url,
                app_public_base_url=public_base_url,
            ),
            timeout_seconds=max(
                1.0,
                _parse_float(
                    os.getenv(
                        "STORAGE_TIMEOUT_SECONDS",
                        os.getenv("KOKORO_STORAGE_TIMEOUT_SECONDS"),
                    ),
                    default=30.0,
                ),
            ),
        )
        if deployment_mode == "container" and storage.provider != "http":
            raise ValueError(
                "KOKORO_DEPLOYMENT_MODE=container requires "
                "KOKORO_STORAGE_PROVIDER=http",
            )
        if storage.provider == "local":
            raise ValueError(
                "KOKORO_STORAGE_PROVIDER=local is no longer supported; "
                "use KOKORO_STORAGE_PROVIDER=http",
            )
        if storage.provider not in {"http", "memory"}:
            raise ValueError(
                "KOKORO_STORAGE_PROVIDER must be http",
            )
        if storage.provider == "http" and not storage.base_url:
            raise ValueError(
                "KOKORO_STORAGE_PROVIDER=http requires "
                "KOKORO_STORAGE_BASE_URL",
            )
        if storage.provider == "http" and not storage.api_key:
            raise ValueError(
                "KOKORO_STORAGE_PROVIDER=http requires "
                "KOKORO_STORAGE_API_KEY",
            )
        if storage.provider == "http" and not storage.public_base_url:
            raise ValueError(
                "KOKORO_STORAGE_PROVIDER=http requires "
                "KOKORO_STORAGE_PUBLIC_BASE_URL",
            )

        cloud = _load_cloud_settings()
        web_push = _load_web_push_settings()
        auth = AuthSettings.from_env()
        if cloud.active:
            auth = AuthSettings(
                enabled=True,
                jwt_secret=auth.jwt_secret,
                jwt_ttl_seconds=cloud.session_ttl_seconds,
                bootstrap_admin_email="",
                bootstrap_admin_password="",
            )

        _warn_deprecated_env()

        return cls(
            default_provider_id=default_provider_id,
            database_url=database_url,
            deployment_mode=deployment_mode,
            auth=auth,
            cloud=cloud,
            demo_oauth=DemoOAuthSettings.from_env(),
            web_push=web_push,
            config_encryption_key=(
                os.getenv("CONFIG_ENCRYPTION_KEY", "").strip()
                or os.getenv("KOKORO_CONFIG_ENCRYPTION_KEY", "").strip()
            ),
            openai_compatible_providers=tuple(providers),
            embedding=embedding,
            storage=storage,
            image_api=image_api,
            video_api=video_api,
            comfyui=comfyui,
            openai_image=openai_image,
            image_provider=image_provider,
            image_profiles_raw=image_profiles_raw,
            video_profiles_raw=video_profiles_raw,
            tts=tts,
            tavily=tavily,
            web_fetch=web_fetch,
            auto_consolidation=auto_consolidation,
            world_events=world_events,
            anthropic=anthropic,
            user_timezone=user_timezone,
            default_primary_language=default_primary_language,
            calendar=calendar,
            weather=weather,
            geoip=geoip,
            nsfw_mode=_load_nsfw_mode_settings(),
            whatsapp_sidecar=whatsapp_sidecar,
            persona=persona,
            humanization=HumanizationSettings.from_env(),
            prompt_quality=PromptQualitySettings.from_env(),
            memoir=MemoirSettings.from_env(),
            public_base_url=public_base_url,
            uploads_dir=uploads_dir,
            debug_ui_enabled=_parse_bool(
                os.getenv("KOKORO_DEBUG_UI_ENABLED"), default=False,
            ),
        )

    def build_openai_compatible_models(self) -> list[OpenAICompatibleChatModel]:  # noqa: E501
        return _do_build_openai_compatible_models(self)


def _load_cloud_settings() -> CloudSettings:
    enabled = _parse_bool(os.getenv("YURALUME_CLOUD_ENABLED"), default=False)
    user_service_url = (
        os.getenv("YURALUME_CLOUD_USER_SERVICE_URL", "").strip().rstrip("/")
    )
    gateway_url = (
        os.getenv("YURALUME_CLOUD_GATEWAY_URL", "").strip().rstrip("/")
    )
    deployment_token = os.getenv(
        "YURALUME_CLOUD_DEPLOYMENT_TOKEN", "",
    ).strip()
    deployment_id = os.getenv(
        "YURALUME_CLOUD_DEPLOYMENT_ID", "",
    ).strip()
    deployment_audience = os.getenv(
        "YURALUME_CLOUD_DEPLOYMENT_AUDIENCE", "",
    ).strip()
    hosted_play_internal_token = os.getenv(
        "YURALUME_CLOUD_HOSTED_PLAY_INTERNAL_TOKEN", "",
    ).strip()
    internal_service_credential = os.getenv(
        "YURALUME_CLOUD_USER_INTERNAL_CREDENTIAL", "",
    ).strip()
    settings = CloudSettings(
        enabled=enabled,
        user_service_url=user_service_url,
        gateway_url=gateway_url,
        deployment_token=deployment_token,
        deployment_id=deployment_id,
        deployment_audience=deployment_audience,
        hosted_play_internal_token=hosted_play_internal_token,
        internal_service_credential=internal_service_credential,
        introspect_timeout=max(
            0.5,
            _parse_float(
                os.getenv("YURALUME_CLOUD_INTROSPECT_TIMEOUT"),
                default=5.0,
            ),
        ),
        session_ttl_seconds=max(
            60,
            _parse_int(
                os.getenv("YURALUME_CLOUD_SESSION_TTL_SECONDS"),
                default=3600,
            ),
        ),
        llm_model_presets=_parse_cloud_llm_presets(
            os.getenv("YURALUME_CLOUD_LLM_PRESETS"),
        ),
        image_preset=(
            os.getenv("YURALUME_CLOUD_IMAGE_PRESET", "").strip()
            or "yuralume-anime"
        ),
        video_preset=(
            os.getenv("YURALUME_CLOUD_VIDEO_PRESET", "").strip()
            or "yuralume-anime"
        ),
        tts_voice_default=os.getenv("YURALUME_CLOUD_TTS_VOICE", "").strip(),
        runtime_config_enabled=_parse_bool(
            os.getenv("YURALUME_CLOUD_RUNTIME_CONFIG_ENABLED"),
            default=False,
        ),
        runtime_config_internal_token=os.getenv(
            "YURALUME_CLOUD_RUNTIME_CONFIG_INTERNAL_TOKEN", "",
        ).strip(),
        require_paid_tier=_parse_bool(
            os.getenv("YURALUME_CLOUD_REQUIRE_PAID_TIER"),
            default=False,
        ),
    )
    if not settings.active:
        return settings
    missing = [
        name
        for name, value in (
            ("YURALUME_CLOUD_USER_SERVICE_URL", settings.user_service_url),
            ("YURALUME_CLOUD_GATEWAY_URL", settings.gateway_url),
            ("YURALUME_CLOUD_DEPLOYMENT_TOKEN", settings.deployment_token),
            ("YURALUME_CLOUD_DEPLOYMENT_ID", settings.deployment_id),
            ("YURALUME_CLOUD_DEPLOYMENT_AUDIENCE", settings.deployment_audience),
            ("YURALUME_CLOUD_USER_INTERNAL_CREDENTIAL", settings.internal_service_credential),
        )
        if not value
    ]
    if missing:
        raise RuntimeError(
            "YURALUME_CLOUD_ENABLED=true requires "
            + ", ".join(missing),
        )
    return settings


def _load_web_push_settings() -> WebPushSettings:
    return WebPushSettings(
        vapid_public_key=(
            os.getenv("WEB_PUSH_VAPID_PUBLIC_KEY", "").strip()
            or os.getenv("KOKORO_WEB_PUSH_VAPID_PUBLIC_KEY", "").strip()
        ),
        vapid_private_key=(
            os.getenv("WEB_PUSH_VAPID_PRIVATE_KEY", "").strip()
            or os.getenv("KOKORO_WEB_PUSH_VAPID_PRIVATE_KEY", "").strip()
        ),
        vapid_subject=(
            os.getenv("WEB_PUSH_VAPID_SUBJECT", "").strip()
            or os.getenv("KOKORO_WEB_PUSH_VAPID_SUBJECT", "").strip()
            or "mailto:admin@example.invalid"
        ),
        ttl_seconds=_parse_int(
            os.getenv("WEB_PUSH_TTL_SECONDS")
            or os.getenv("KOKORO_WEB_PUSH_TTL_SECONDS"),
            default=86400,
        ),
    )


def _parse_cloud_llm_presets(raw: str | None) -> dict[str, str]:
    if raw is None or not raw.strip():
        return {}
    presets: dict[str, str] = {}
    for item in raw.split(","):
        key, separator, value = item.partition("=")
        feature_key = key.strip()
        model = value.strip()
        if separator and feature_key and model:
            presets[feature_key] = model
    return presets


def _load_weather_settings() -> WeatherSettings:
    """Parse weather env vars. Missing lat / lon collapses to disabled
    (provider falls back to :class:`NullWeatherProvider` in container).

    Invalid floats are tolerated: we log nothing here because container
    construction is non-fatal — the null provider will emit empty
    blocks and the rest of the system keeps working. Operators see the
    silent absence and check ``KOKORO_WEATHER_LATITUDE`` etc.
    """
    enabled = _parse_bool(
        os.getenv("WEATHER_ENABLED", os.getenv("KOKORO_WEATHER_ENABLED")),
        default=True,
    )
    latitude = _parse_optional_float(
        os.getenv("WEATHER_LATITUDE", os.getenv("KOKORO_WEATHER_LATITUDE")),
    )
    longitude = _parse_optional_float(
        os.getenv("WEATHER_LONGITUDE", os.getenv("KOKORO_WEATHER_LONGITUDE")),
    )
    label = (
        os.getenv("WEATHER_LOCATION_LABEL", "")
        or os.getenv("KOKORO_WEATHER_LOCATION_LABEL", "")
        or ""
    ).strip()
    tz_id = (
        os.getenv("WEATHER_TIMEZONE", "")
        or os.getenv("KOKORO_WEATHER_TIMEZONE", "")
        or "auto"
    ).strip() or "auto"
    ttl = max(
        60,
        _parse_int(
            os.getenv(
                "WEATHER_CACHE_TTL_SECONDS",
                os.getenv("KOKORO_WEATHER_CACHE_TTL_SECONDS"),
            ),
            default=15 * 60,
        ),
    )
    return WeatherSettings(
        enabled=enabled,
        latitude=latitude,
        longitude=longitude,
        # Keep empty when unset — localization happens where the deployment
        # language is known (see _build_weather_provider), not as a raw literal.
        location_label=label,
        timezone_id=tz_id,
        cache_ttl_seconds=ttl,
    )


def _load_geoip_settings() -> GeoIpSettings:
    enabled = _parse_bool(
        os.getenv("GEOIP_ENABLED", os.getenv("KOKORO_GEOIP_ENABLED")),
        default=True,
    )
    provider = (
        os.getenv("GEOIP_PROVIDER", "")
        or os.getenv("KOKORO_GEOIP_PROVIDER", "")
        or "ip-api"
    ).strip().lower()
    endpoint = (
        os.getenv("GEOIP_ENDPOINT", "")
        or os.getenv("KOKORO_GEOIP_ENDPOINT", "")
        or "http://ip-api.com/json/"
    ).strip()
    ttl = max(
        60,
        _parse_int(
            os.getenv(
                "GEOIP_CACHE_TTL_SECONDS",
                os.getenv("KOKORO_GEOIP_CACHE_TTL_SECONDS"),
            ),
            default=24 * 60 * 60,
        ),
    )
    timeout = max(
        0.5,
        _parse_float(
            os.getenv(
                "GEOIP_TIMEOUT_SECONDS",
                os.getenv("KOKORO_GEOIP_TIMEOUT_SECONDS"),
            ),
            default=3.0,
        ),
    )
    return GeoIpSettings(
        enabled=enabled,
        provider=provider or "ip-api",
        endpoint=endpoint or "http://ip-api.com/json/",
        cache_ttl_seconds=ttl,
        timeout_seconds=timeout,
    )


def _load_nsfw_mode_settings() -> NsfwModeSettings:
    ttl = _parse_int(
        os.getenv(
            "NSFW_MODE_TTL_SECONDS",
            os.getenv("KOKORO_NSFW_MODE_TTL_SECONDS"),
        ),
        default=30 * 60,
    )
    return NsfwModeSettings(ttl_seconds=max(60, ttl))


def _load_whatsapp_sidecar_settings() -> WhatsAppSidecarSettings:
    base_url = (
        os.getenv("WHATSAPP_SIDECAR_URL", "")
        or os.getenv("KOKORO_WHATSAPP_SIDECAR_URL", "")
        or "http://whatsapp-sidecar:32190"
    ).strip().rstrip("/")
    api_token = (
        os.getenv("WHATSAPP_SIDECAR_API_TOKEN", "")
        or os.getenv("KOKORO_WHATSAPP_SIDECAR_API_TOKEN", "")
    ).strip()
    return WhatsAppSidecarSettings(
        base_url=base_url or "http://whatsapp-sidecar:32190",
        api_token=api_token,
    )


def _load_user_timezone_settings() -> UserTimezoneSettings:
    raw = (
        os.getenv("USER_TIMEZONE", "")
        or os.getenv("KOKORO_USER_TIMEZONE", "")
        or "UTC"
    )
    try:
        timezone_id = normalise_timezone_id(raw)
    except ValueError as exc:
        raise ValueError(
            "KOKORO_USER_TIMEZONE / USER_TIMEZONE must be an IANA timezone "
            "id such as 'UTC' or 'Asia/Taipei'",
        ) from exc
    return UserTimezoneSettings(default_timezone_id=timezone_id)


def _load_default_primary_language() -> str:
    """Deploy-time default language tag (``USER_PRIMARY_LANGUAGE``).

    Parallel to ``_load_user_timezone_settings``: unprefixed name wins,
    ``KOKORO_`` stays as a migration fallback, and an empty value falls
    back to the project default (``zh-TW``). A structurally broken tag
    raises so a typo surfaces at boot instead of silently shipping the
    wrong language to the LLM content layer."""
    raw = (
        os.getenv("USER_PRIMARY_LANGUAGE", "")
        or os.getenv("KOKORO_USER_PRIMARY_LANGUAGE", "")
        or DEFAULT_PRIMARY_LANGUAGE
    )
    try:
        return normalise_language_tag(raw)
    except ValueError as exc:
        raise ValueError(
            "KOKORO_USER_PRIMARY_LANGUAGE / USER_PRIMARY_LANGUAGE must be a "
            "BCP 47 language tag such as 'zh-TW', 'en-US', or 'ja-JP'",
        ) from exc


def _parse_optional_float(raw: str | None) -> float | None:
    if raw is None:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _load_persona_settings() -> PersonaSettings:
    """Parse persona env knobs. Bounds-check each numeric so a typo
    in env doesn't translate to absurd thresholds (e.g. a negative
    decay window that immediately stales every field)."""
    enabled = _parse_bool(os.getenv("KOKORO_PERSONA_ENABLED"), default=True)
    extraction_bg = _parse_bool(
        os.getenv("KOKORO_PERSONA_EXTRACTION_IN_BACKGROUND"), default=True,
    )
    curiosity_enabled = _parse_bool(
        os.getenv(
            "PERSONA_CURIOSITY_ENABLED",
            os.getenv("KOKORO_PERSONA_CURIOSITY_ENABLED"),
        ),
        default=True,
    )
    curiosity_proactive_enabled = _parse_bool(
        os.getenv(
            "PERSONA_CURIOSITY_PROACTIVE_ENABLED",
            os.getenv("KOKORO_PERSONA_CURIOSITY_PROACTIVE_ENABLED"),
        ),
        default=True,
    )
    return PersonaSettings(
        enabled=enabled,
        curiosity_enabled=curiosity_enabled,
        curiosity_proactive_enabled=curiosity_proactive_enabled,
        extraction_in_background=extraction_bg,
        familiarity_stranger_max_msgs=max(
            1,
            _parse_int(os.getenv("KOKORO_PERSONA_FAMILIARITY_STRANGER_MAX_MSGS"), default=30),
        ),
        familiarity_stranger_max_days=max(
            0,
            _parse_int(os.getenv("KOKORO_PERSONA_FAMILIARITY_STRANGER_MAX_DAYS"), default=2),
        ),
        familiarity_acquaintance_max_msgs=max(
            2,
            _parse_int(os.getenv("KOKORO_PERSONA_FAMILIARITY_ACQUAINTANCE_MAX_MSGS"), default=200),
        ),
        familiarity_acquaintance_max_days=max(
            1,
            _parse_int(os.getenv("KOKORO_PERSONA_FAMILIARITY_ACQUAINTANCE_MAX_DAYS"), default=14),
        ),
        familiarity_close_min_days=max(
            1,
            _parse_int(os.getenv("KOKORO_PERSONA_FAMILIARITY_CLOSE_MIN_DAYS"), default=30),
        ),
        recent_activity_frequent_min_7d=max(
            1,
            _parse_int(os.getenv("KOKORO_PERSONA_FREQUENT_MIN_7D"), default=50),
        ),
        recent_activity_inactive_max_7d=max(
            0,
            _parse_int(os.getenv("KOKORO_PERSONA_INACTIVE_MAX_7D"), default=5),
        ),
        dream_quiet_hours_start=_clamp_hour(
            _parse_int(os.getenv("KOKORO_PERSONA_DREAM_QUIET_START"), default=23),
        ),
        dream_quiet_hours_end=_clamp_hour(
            _parse_int(os.getenv("KOKORO_PERSONA_DREAM_QUIET_END"), default=7),
        ),
        dream_min_pending=max(
            1,
            _parse_int(os.getenv("KOKORO_PERSONA_DREAM_MIN_PENDING"), default=5),
        ),
        dream_min_interval_hours=max(
            1,
            _parse_int(os.getenv("KOKORO_PERSONA_DREAM_MIN_INTERVAL_HOURS"), default=6),
        ),
        decay_after_days=max(
            1,
            _parse_int(os.getenv("KOKORO_PERSONA_DECAY_AFTER_DAYS"), default=60),
        ),
        stale_after_days=max(
            2,
            _parse_int(os.getenv("KOKORO_PERSONA_STALE_AFTER_DAYS"), default=180),
        ),
        interaction_strength_cache_ttl_seconds=max(
            1.0,
            _parse_float(
                os.getenv("KOKORO_PERSONA_INTERACTION_CACHE_TTL_SECONDS"),
                default=60.0,
            ),
        ),
    )


def _clamp_hour(value: int) -> int:
    if value < 0:
        return 0
    if value > 23:
        return 23
    return value


def _load_auto_consolidation_settings(
    default_provider_id: str,
) -> AutoConsolidationSettings:
    default_enabled = default_provider_id != "fake"
    enabled = _parse_bool(
        os.getenv("KOKORO_AUTO_CONSOLIDATION_ENABLED"),
        default=default_enabled,
    )
    threshold = _parse_int(
        os.getenv("KOKORO_AUTO_CONSOLIDATION_THRESHOLD"), default=200,
    )
    cooldown_hours = _parse_float(
        os.getenv("KOKORO_AUTO_CONSOLIDATION_COOLDOWN_HOURS"), default=6.0,
    )
    return AutoConsolidationSettings(
        enabled=enabled,
        threshold=max(1, threshold),
        cooldown_hours=max(0.0, cooldown_hours),
    )


def _parse_bool(raw: str | None, *, default: bool) -> bool:
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _resolve_storage_public_base_url(
    *, configured: str, app_public_base_url: str,
) -> str:
    if app_public_base_url and (not configured or _is_loopback_url(configured)):
        return app_public_base_url
    return configured


def _is_loopback_url(raw: str) -> bool:
    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return host in {"localhost", "0.0.0.0", "::1"} or host.startswith("127.")


def _parse_int(raw: str | None, *, default: int) -> int:
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _parse_float(raw: str | None, *, default: float) -> float:
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _media_api_base_url(
    *,
    explicit: str,
    provider: str,
    media_kind: str,
) -> str:
    if explicit:
        return explicit.rstrip("/")
    if provider in {"openai", "gpt_image", "gpt-image"}:
        return os.getenv("KOKORO_OPENAI_BASE_URL", "").strip().rstrip("/") or (
            "https://api.openai.com/v1"
        )
    if provider in {"xai", "grok", "grok_image", "grok-image"}:
        return "https://api.x.ai/v1"
    if provider in {
        "gemini",
        "google",
        "nano_banana",
        "nano-banana",
        "google_veo",
        "gemini_veo",
        "veo",
    }:
        return "https://generativelanguage.googleapis.com/v1beta"
    if media_kind == "image":
        return ""
    return ""


def _media_api_default_model(*, provider: str, media_kind: str) -> str:
    if media_kind == "image":
        if provider in {"openai", "gpt_image", "gpt-image"}:
            return "gpt-image-2"
        if provider in {"xai", "grok", "grok_image", "grok-image"}:
            return "grok-imagine-image-quality"
        if provider in {"gemini", "google", "nano_banana", "nano-banana"}:
            return "gemini-2.5-flash-image"
        return "gpt-image2"
    if provider in {"google", "google_veo", "gemini_veo", "veo"}:
        return "veo-3.1-generate-preview"
    return "veo3"


def _do_build_openai_compatible_models(
    settings: "AppSettings",
) -> list[OpenAICompatibleChatModel]:
    models: list[OpenAICompatibleChatModel] = []
    for provider in settings.openai_compatible_providers:
        raw_max_tokens = provider.get("max_tokens")
        max_tokens: int | None
        if raw_max_tokens is None:
            max_tokens = None
        else:
            try:
                max_tokens = int(raw_max_tokens)
            except (TypeError, ValueError):
                max_tokens = None
        models.append(
            OpenAICompatibleChatModel(
                provider_id=str(provider["provider_id"]),
                base_url=str(provider["base_url"]),
                api_key=provider.get("api_key"),
                model=str(provider["model"]),
                supports_vision=bool(provider.get("supports_vision", False)),
                max_tokens=max_tokens,
            )
        )
    return models


def _load_world_event_settings() -> WorldEventSettings:
    """Parse world-event feed config from environment variables.

    Feeds are numbered starting at 1:
      KOKORO_WORLD_EVENT_FEED_1_SOURCE_ID=mynews
      KOKORO_WORLD_EVENT_FEED_1_URL=https://example.com/rss
      KOKORO_WORLD_EVENT_FEED_1_TAGS=technology,ai

    Gaps in numbering stop the scan at the first missing SOURCE_ID.
    """
    feeds: list[WorldEventFeed] = []
    for i in range(1, 21):
        source_id = os.getenv(f"KOKORO_WORLD_EVENT_FEED_{i}_SOURCE_ID", "").strip()
        if not source_id:
            break
        url = os.getenv(f"KOKORO_WORLD_EVENT_FEED_{i}_URL", "").strip()
        if not url:
            continue
        raw_tags = os.getenv(f"KOKORO_WORLD_EVENT_FEED_{i}_TAGS", "")
        tags = tuple(t.strip() for t in raw_tags.split(",") if t.strip())
        feeds.append(WorldEventFeed(source_id=source_id, url=url, topic_tags=tags))

    retention = _parse_int(
        os.getenv("KOKORO_WORLD_EVENT_RETENTION_DAYS"), default=30,
    )
    interval = _parse_float(
        os.getenv("KOKORO_WORLD_EVENT_SCHEDULER_INTERVAL"), default=3600.0,
    )
    return WorldEventSettings(
        feeds=tuple(feeds),
        retention_days=max(1, retention),
        scheduler_interval_seconds=max(60.0, interval),
    )
