"""Provider catalog used by BYOK admin settings."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ProviderFieldSpec:
    key: str
    label: str
    kind: str = "text"
    required: bool = False
    required_for_capabilities: tuple[str, ...] = ()
    placeholder: str = ""
    secret: bool = False
    advanced: bool = False


@dataclass(frozen=True, slots=True)
class ProviderCatalogEntry:
    id: str
    display_name: str
    capabilities: tuple[str, ...]
    auth_fields: tuple[ProviderFieldSpec, ...] = field(default_factory=tuple)
    config_fields: tuple[ProviderFieldSpec, ...] = field(default_factory=tuple)
    model_catalog_mode: str = "manual"
    default_models: tuple[str, ...] = field(default_factory=tuple)
    adapter_kind: str = ""
    docs_url: str = ""


def list_provider_catalog() -> tuple[ProviderCatalogEntry, ...]:
    api_key = ProviderFieldSpec(
        key="api_key",
        label="API key",
        kind="password",
        required=True,
        secret=True,
        placeholder="sk-...",
    )
    optional_api_key = ProviderFieldSpec(
        key="api_key",
        label="API key",
        kind="password",
        required=False,
        secret=True,
        placeholder="sk-...",
    )
    base_url = ProviderFieldSpec(
        key="base_url",
        label="Base URL",
        placeholder="https://api.example.com/v1",
        advanced=True,
    )
    required_base_url = ProviderFieldSpec(
        key="base_url",
        label="Base URL",
        placeholder="https://api.example.com/v1",
        required=True,
        advanced=True,
    )
    default_model = ProviderFieldSpec(
        key="default_model",
        label="Default model",
        placeholder="provider default",
        # LLM accepts empty for providers that have a server-side default.
        # Media/TTS use capability-specific fields below so one OpenAI row
        # never has to reuse an image or TTS model as the chat model.
        required_for_capabilities=("video",),
    )
    required_default_model = ProviderFieldSpec(
        key="default_model",
        label="Default model",
        required=True,
        placeholder="provider default",
    )
    voice_id = ProviderFieldSpec(
        key="voice_id",
        label="Default voice",
        placeholder="alloy",
    )
    image_model = ProviderFieldSpec(
        key="image_model",
        label="Image model",
        placeholder="gpt-image-2",
        advanced=True,
        required_for_capabilities=("image",),
    )
    tts_model = ProviderFieldSpec(
        key="tts_model",
        label="TTS model",
        placeholder="gpt-4o-mini-tts",
        advanced=True,
        required_for_capabilities=("tts",),
    )
    timeout_seconds = ProviderFieldSpec(
        key="timeout_seconds",
        label="Timeout seconds",
        kind="number",
        placeholder="180",
        advanced=True,
    )
    supports_vision = ProviderFieldSpec(
        key="supports_vision",
        label="Supports vision",
        kind="checkbox",
        advanced=True,
    )
    max_tokens = ProviderFieldSpec(
        key="max_tokens",
        label="Max tokens",
        kind="number",
        placeholder="4096",
        advanced=True,
    )
    anthropic_version = ProviderFieldSpec(
        key="anthropic_version",
        label="Anthropic version",
        placeholder="2023-06-01",
        advanced=True,
    )
    response_format = ProviderFieldSpec(
        key="response_format",
        label="Response format",
        placeholder="wav",
        advanced=True,
    )
    embedding_model = ProviderFieldSpec(
        key="embedding_model",
        label="Embedding model",
        placeholder="text-embedding-bge-m3",
        advanced=True,
        required_for_capabilities=("embedding",),
    )
    # OpenRouter is an aggregator whose embedding line-up shifts with its
    # upstreams, so the label/placeholder pin the memory-store hard
    # constraint: embeddings MUST come out at exactly 1024 dims
    # (MEMORY_EMBEDDING_DIM). baai/bge-m3 (verified 2026-07-05) is
    # natively 1024-dim; any other model must either be natively
    # 1024-dim or support the OpenAI `dimensions` truncation param
    # (toggle "Request dimensions"). Guidance rides label/placeholder
    # because ProviderFieldSpec has no separate hint slot — same
    # precedent as the reasoning-controls fields above.
    openrouter_embedding_model = ProviderFieldSpec(
        key="embedding_model",
        label="Embedding model (must output 1024 dims — see placeholder)",
        placeholder="baai/bge-m3 (原生 1024 維)；他型需支援 dimensions 截斷並勾 Request dimensions",
        advanced=True,
        required_for_capabilities=("embedding",),
    )
    embedding_dimension = ProviderFieldSpec(
        key="embedding_dimension",
        label="Embedding dimension",
        kind="number",
        placeholder="1024",
        advanced=True,
        required_for_capabilities=("embedding",),
    )
    request_dimensions = ProviderFieldSpec(
        key="request_dimensions",
        label="Request dimensions",
        kind="checkbox",
        advanced=True,
    )
    # Reasoning / thinking controls (all opt-in, default absent = send
    # nothing, behaviour identical to today). openai_compatible family
    # gets disable_reasoning / reasoning_effort / extra_request_params /
    # strip_think_tags; anthropic gets thinking_budget_tokens. Different
    # adapter kinds surface different field sets so incompatible params
    # are never offered to a provider that can't use them.
    disable_reasoning = ProviderFieldSpec(
        key="disable_reasoning",
        label="Disable reasoning / thinking",
        kind="checkbox",
        advanced=True,
    )
    reasoning_effort = ProviderFieldSpec(
        key="reasoning_effort",
        label="Reasoning effort (provider-specific value, leave blank for default)",
        placeholder="e.g. low / medium / high",
        advanced=True,
    )
    extra_request_params = ProviderFieldSpec(
        key="extra_request_params",
        label="Extra request params (raw JSON object, merged into request payload)",
        placeholder='Advanced: per-provider params, e.g. {"top_k": 40}',
        advanced=True,
    )
    strip_think_tags = ProviderFieldSpec(
        key="strip_think_tags",
        label="Strip <think>...</think> tags from replies",
        kind="checkbox",
        advanced=True,
    )
    thinking_budget_tokens = ProviderFieldSpec(
        key="thinking_budget_tokens",
        label="Thinking budget tokens (blank = extended thinking off)",
        kind="number",
        placeholder="e.g. 4096",
        advanced=True,
    )
    # Web-search (`search` capability) fields. `max_results` caps snippets
    # per call; `search_depth` is Tavily-specific (basic/advanced).
    # `searxng_base_url` carries the SearXNG operator gotcha in its
    # label/placeholder because ProviderFieldSpec has no separate hint slot
    # — same precedent as the reasoning / OpenRouter-embedding fields above.
    search_max_results = ProviderFieldSpec(
        key="max_results",
        label="Max results",
        kind="number",
        placeholder="5",
        advanced=True,
    )
    search_depth = ProviderFieldSpec(
        key="search_depth",
        label="Search depth (basic / advanced)",
        placeholder="advanced",
        advanced=True,
    )
    searxng_base_url = ProviderFieldSpec(
        key="base_url",
        label="Base URL (SearXNG instance; must enable json format)",
        placeholder="https://searxng.example.com — settings.yml search.formats 需含 json",
        required=True,
    )
    # OpenAI Responses built-in web search (`search` capability, LLM-native).
    # `search_model` picks the (cheap) model that does the search+synthesis;
    # `search_tool_type` reconciles the built-in tool name with that model
    # (GA `web_search` vs older `web_search_preview`); `search_context_size`
    # is the cost/latency knob. Guidance rides label/placeholder because
    # ProviderFieldSpec has no separate hint slot — same precedent as the
    # reasoning / SearXNG fields above.
    search_model = ProviderFieldSpec(
        key="search_model",
        label="Search model",
        placeholder="挑便宜小模型即可，如 gpt-5.4-mini",
        required_for_capabilities=("search",),
    )
    search_context_size = ProviderFieldSpec(
        key="search_context_size",
        label="Search context size (low / medium / high — 影響成本與延遲)",
        placeholder="low",
        advanced=True,
    )
    search_tool_type = ProviderFieldSpec(
        key="search_tool_type",
        label="Web search tool type (須與所選模型相容)",
        placeholder="web_search（舊模型用 web_search_preview）",
        advanced=True,
    )
    # ComfyUI direct-connect (`image` capability, kind=comfyui). `server`
    # is the ComfyUI HTTP endpoint; `checkpoint` uses kind="comfyui_checkpoint"
    # so the admin form renders a searchable dropdown populated from
    # GET /system/comfyui/checkpoints (falls back to a plain text input when
    # the ComfyUI /object_info fetch fails — see plan risk note). The other
    # knobs mirror ComfyProfileConfig so one row = one (server, checkpoint,
    # workflow) profile.
    comfyui_server = ProviderFieldSpec(
        key="server",
        label="ComfyUI server URL",
        placeholder="http://127.0.0.1:8188",
        required=True,
    )
    comfyui_checkpoint = ProviderFieldSpec(
        key="checkpoint",
        label="Checkpoint (model file)",
        kind="comfyui_checkpoint",
        placeholder="waiNSFWIllustrious_v140.safetensors",
    )
    comfyui_workflow_file = ProviderFieldSpec(
        key="workflow_file",
        label="Workflow JSON path (blank = built-in default)",
        placeholder="/path/to/workflow_api.json",
        advanced=True,
    )
    comfyui_lora_dir = ProviderFieldSpec(
        key="lora_dir",
        label="LoRA directory (ComfyUI models/loras path)",
        placeholder="/comfyui/models/loras",
        advanced=True,
    )
    return (
        ProviderCatalogEntry(
            id="openai",
            display_name="OpenAI",
            capabilities=("llm", "embedding", "image", "tts"),
            auth_fields=(api_key,),
            config_fields=(
                base_url,
                default_model,
                image_model,
                embedding_model,
                embedding_dimension,
                request_dimensions,
                tts_model,
                voice_id,
                timeout_seconds,
                supports_vision,
                max_tokens,
                response_format,
                disable_reasoning,
                reasoning_effort,
                extra_request_params,
                strip_think_tags,
            ),
            model_catalog_mode="remote",
            default_models=(
                "gpt-4o-mini",
            ),
            adapter_kind="openai",
            docs_url="https://platform.openai.com/docs",
        ),
        ProviderCatalogEntry(
            id="anthropic",
            display_name="Anthropic",
            capabilities=("llm",),
            auth_fields=(api_key,),
            config_fields=(
                base_url,
                default_model,
                anthropic_version,
                supports_vision,
                max_tokens,
                thinking_budget_tokens,
            ),
            model_catalog_mode="manual",
            default_models=("claude-sonnet-4-5",),
            adapter_kind="anthropic",
            docs_url="https://docs.anthropic.com",
        ),
        ProviderCatalogEntry(
            id="google_gemini",
            display_name="Google Gemini",
            capabilities=("llm", "image"),
            auth_fields=(api_key,),
            config_fields=(
                base_url,
                default_model,
                timeout_seconds,
                supports_vision,
                max_tokens,
            ),
            model_catalog_mode="manual",
            default_models=("gemini-2.0-flash", "gemini-2.5-flash-image"),
            adapter_kind="google_gemini",
            docs_url="https://ai.google.dev/gemini-api/docs",
        ),
        ProviderCatalogEntry(
            id="xai",
            display_name="xAI",
            capabilities=("image",),
            auth_fields=(api_key,),
            config_fields=(base_url, default_model, timeout_seconds),
            model_catalog_mode="manual",
            default_models=("grok-2-image-1212",),
            adapter_kind="xai",
            docs_url="https://docs.x.ai",
        ),
        ProviderCatalogEntry(
            id="comfyui",
            display_name="ComfyUI (self-hosted)",
            # Direct-connect local image generation. No auth (the ComfyUI
            # HTTP API is unauthenticated on the LAN); config carries the
            # (server, checkpoint, workflow, lora_dir) tuple that becomes a
            # kind=comfyui ImageProfile in runtime_sync._sync_image_profiles.
            capabilities=("image",),
            config_fields=(
                comfyui_server,
                comfyui_checkpoint,
                comfyui_workflow_file,
                comfyui_lora_dir,
                timeout_seconds,
            ),
            model_catalog_mode="manual",
            adapter_kind="comfyui",
            docs_url="https://docs.comfy.org",
        ),
        ProviderCatalogEntry(
            id="google_veo",
            display_name="Google Veo",
            capabilities=("video",),
            auth_fields=(api_key,),
            config_fields=(base_url, default_model, timeout_seconds),
            model_catalog_mode="manual",
            default_models=("veo-3.1-generate-preview",),
            adapter_kind="google_veo",
            docs_url="https://ai.google.dev/gemini-api/docs/video",
        ),
        ProviderCatalogEntry(
            id="openrouter",
            display_name="OpenRouter",
            # Full-capability wiring (owner ratified 2026-07-05). llm rides
            # openai_compatible; embedding rides LMStudioEmbedder against
            # OpenRouter's OpenAI-compatible /embeddings; tts rides the
            # OpenAI-speech protocol set; image rides the dedicated
            # OpenRouterImageProvider (OpenRouter posts /api/v1/images, not
            # OpenAI's /images/generations). video is intentionally NOT
            # listed — OpenRouter's video API is async job-based (POST
            # /api/v1/videos → poll job → download content), incompatible
            # with the sync ExternalVideoApiProvider; wiring it needs a new
            # polling adapter (verified 2026-07-05, see plan decision table).
            capabilities=("llm", "embedding", "tts", "image"),
            auth_fields=(api_key,),
            config_fields=(
                base_url,
                default_model,
                image_model,
                openrouter_embedding_model,
                embedding_dimension,
                request_dimensions,
                tts_model,
                voice_id,
                response_format,
                supports_vision,
                max_tokens,
                disable_reasoning,
                reasoning_effort,
                extra_request_params,
                strip_think_tags,
            ),
            model_catalog_mode="remote",
            default_models=("openai/gpt-4o-mini",),
            adapter_kind="openai_compatible",
            docs_url="https://openrouter.ai/docs",
        ),
        ProviderCatalogEntry(
            id="nanogpt",
            display_name="NanoGPT",
            # Built-in preset (owner ratified 2026-07-05). Both capabilities
            # ride existing adapters: llm on openai_compatible, image on the
            # gateway ExternalImageApiProvider (NanoGPT's OpenAI-compatible
            # /v1/images/generations returns b64_json by default — verified
            # 2026-07-05 against docs.nano-gpt.com and the user's own
            # first-hand success report).
            capabilities=("llm", "image"),
            auth_fields=(api_key,),
            config_fields=(
                base_url,
                default_model,
                image_model,
                supports_vision,
                max_tokens,
                timeout_seconds,
                disable_reasoning,
                reasoning_effort,
                extra_request_params,
                strip_think_tags,
            ),
            model_catalog_mode="remote",
            default_models=("gpt-5.2",),
            adapter_kind="openai_compatible",
            docs_url="https://docs.nano-gpt.com",
        ),
        ProviderCatalogEntry(
            id="deepseek",
            display_name="DeepSeek",
            capabilities=("llm",),
            auth_fields=(api_key,),
            config_fields=(
                base_url,
                default_model,
                supports_vision,
                max_tokens,
                disable_reasoning,
                reasoning_effort,
                extra_request_params,
                strip_think_tags,
            ),
            model_catalog_mode="manual",
            default_models=("deepseek-chat",),
            adapter_kind="openai_compatible",
            docs_url="https://api-docs.deepseek.com",
        ),
        ProviderCatalogEntry(
            id="mistral",
            display_name="Mistral",
            capabilities=("llm",),
            auth_fields=(api_key,),
            config_fields=(
                base_url,
                default_model,
                supports_vision,
                max_tokens,
                disable_reasoning,
                reasoning_effort,
                extra_request_params,
                strip_think_tags,
            ),
            model_catalog_mode="manual",
            default_models=("mistral-small-latest",),
            adapter_kind="openai_compatible",
            docs_url="https://docs.mistral.ai",
        ),
        ProviderCatalogEntry(
            id="custom_openai_compatible",
            display_name="Custom OpenAI-Compatible",
            capabilities=("llm", "embedding"),
            auth_fields=(api_key,),
            config_fields=(
                required_base_url,
                required_default_model,
                embedding_model,
                embedding_dimension,
                request_dimensions,
                supports_vision,
                max_tokens,
                disable_reasoning,
                reasoning_effort,
                extra_request_params,
                strip_think_tags,
            ),
            model_catalog_mode="manual",
            adapter_kind="openai_compatible",
        ),
        ProviderCatalogEntry(
            id="local_openai_compatible",
            display_name="Local OpenAI-Compatible",
            capabilities=("llm", "embedding"),
            auth_fields=(optional_api_key,),
            config_fields=(
                base_url,
                default_model,
                embedding_model,
                embedding_dimension,
                request_dimensions,
                supports_vision,
                max_tokens,
                disable_reasoning,
                reasoning_effort,
                extra_request_params,
                strip_think_tags,
            ),
            model_catalog_mode="manual",
            default_models=("local-model", "text-embedding-bge-m3"),
            adapter_kind="openai_compatible",
        ),
        ProviderCatalogEntry(
            id="custom_media_gateway",
            display_name="Custom Media Gateway",
            capabilities=("image", "video"),
            auth_fields=(api_key,),
            config_fields=(required_base_url, required_default_model, timeout_seconds),
            model_catalog_mode="manual",
            adapter_kind="custom_media_gateway",
        ),
        ProviderCatalogEntry(
            id="custom_tts",
            display_name="Custom TTS Server",
            capabilities=("tts",),
            auth_fields=(optional_api_key,),
            config_fields=(
                required_base_url,
                default_model,
                voice_id,
                timeout_seconds,
            ),
            model_catalog_mode="manual",
            adapter_kind="custom_tts",
        ),
        ProviderCatalogEntry(
            id="tavily",
            display_name="Tavily",
            capabilities=("search",),
            auth_fields=(api_key,),
            config_fields=(
                search_depth,
                search_max_results,
                timeout_seconds,
            ),
            model_catalog_mode="manual",
            adapter_kind="tavily",
            docs_url="https://docs.tavily.com",
        ),
        ProviderCatalogEntry(
            id="searxng",
            display_name="SearXNG (self-hosted)",
            capabilities=("search",),
            # base_url required; api_key optional (only for instances
            # behind an auth proxy).
            auth_fields=(optional_api_key,),
            config_fields=(
                searxng_base_url,
                search_max_results,
                timeout_seconds,
            ),
            model_catalog_mode="manual",
            adapter_kind="searxng",
            docs_url="https://docs.searxng.org/admin/settings/settings_search.html",
        ),
        ProviderCatalogEntry(
            id="duckduckgo",
            display_name="DuckDuckGo (Instant Answer only)",
            # No auth. Instant Answer API only — limited coverage, not
            # full-web search; the display name sets the expectation.
            capabilities=("search",),
            config_fields=(
                search_max_results,
                timeout_seconds,
            ),
            model_catalog_mode="manual",
            adapter_kind="duckduckgo",
            docs_url="https://duckduckgo.com/api",
        ),
        ProviderCatalogEntry(
            id="openai_web_search",
            display_name="OpenAI Web Search (Responses)",
            # LLM-native search: the model runs OpenAI's built-in web_search
            # tool over /v1/responses and returns a fused answer + citations.
            # adapter_kind="openai" so the search_model field can reuse remote
            # model discovery (/v1/models); dispatch to the search client is
            # by provider id in runtime_sync, so this is never treated as an
            # LLM provider despite the shared adapter_kind.
            capabilities=("search",),
            auth_fields=(api_key,),
            config_fields=(
                base_url,
                search_model,
                search_context_size,
                search_tool_type,
                search_max_results,
                timeout_seconds,
            ),
            model_catalog_mode="remote",
            default_models=("gpt-5.4-mini",),
            adapter_kind="openai",
            docs_url="https://platform.openai.com/docs/guides/tools-web-search",
        ),
        ProviderCatalogEntry(
            id="yuralume_cloud",
            display_name="Yuralume Cloud",
            capabilities=("llm", "embedding", "image", "video", "tts"),
            auth_fields=(api_key,),
            config_fields=(
                required_base_url,
                default_model,
                image_model,
                embedding_model,
                embedding_dimension,
                request_dimensions,
                tts_model,
                voice_id,
                timeout_seconds,
            ),
            model_catalog_mode="remote",
            adapter_kind="yuralume_cloud",
        ),
    )


def catalog_by_id() -> dict[str, ProviderCatalogEntry]:
    return {entry.id: entry for entry in list_provider_catalog()}
