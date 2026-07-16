"""Chat application service.

Coordinates character, conversation, memory, goal, and model ports to
handle both synchronous and streaming chat replies. Memory retrieval
uses ``MemoryRanker`` to pick prompt-ready items; post-turn processing
delegates to ``PostTurnProcessorPort`` for combined memory extraction,
LLM-based state refinement, and short-term intent updates. Every
``goal_review_interval`` turns, ``GoalReviewerPort`` is asked to advance
the character's medium-term goals.
"""

import asyncio
import base64
import logging
import mimetypes
import re
from collections.abc import AsyncIterator, Awaitable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone, tzinfo
from inspect import signature
from pathlib import Path
from uuid import uuid4

from kokoro_link.application.dto.chat import (
    ChatReplyResponse,
    ConversationResponse,
    SendChatMessageRequest,
)
from kokoro_link.application.services.account_runtime_profile import (
    PermissiveAccountRuntimeProfileResolver,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)
from kokoro_link.application.services.cloud_identity_context import (
    bind_cloud_actor,
)
from kokoro_link.application.services.auto_consolidation_trigger import (
    AutoConsolidationTrigger,
)
from kokoro_link.application.services.feature_keys import (
    FEATURE_CHAT,
    FEATURE_IMAGE_RECOGNITION,
    FEATURE_NSFW_SAFE_SUMMARY,
)
from kokoro_link.application.services.feed_reaction_memorializer import (
    FeedReactionMemorializer,
)
from kokoro_link.application.services.goal_service import GoalService
from kokoro_link.application.services.location_context import prompt_location_fact
from kokoro_link.application.services.memory_embedding import attach_embeddings
from kokoro_link.application.services.nsfw_mode import (
    CONTENT_MODE_NSFW,
    CONTENT_MODE_NORMAL,
    MEMORY_TAG_NSFW_MODE,
    NsfwModeService,
)
from kokoro_link.application.services.operator_profile_service import (
    OperatorProfileService,
)
from kokoro_link.application.services.operator_persona_service import (
    OperatorPersonaService,
)
from kokoro_link.application.services.character_social_knowledge_service import (
    CharacterSocialKnowledgeService,
)
from kokoro_link.application.services.persona_extraction_service import (
    PersonaExtractionService,
)
from kokoro_link.application.services.persona_curiosity_service import (
    PersonaCuriosityService,
)
from kokoro_link.application.services.persona_curiosity_observability import (
    persona_curiosity_plan_summary,
)
from kokoro_link.application.services.schedule_memorializer import ScheduleMemorializer
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.application.services.tool_call_parser import (
    looks_like_tool_call_attempt, parse_tool_call,
)
from kokoro_link.application.services.tts_pregeneration_service import (
    TTSPregenerationService,
)
from kokoro_link.application.services.turn_snapshot_codec import (
    arc_to_dict, goal_to_dict, schedule_to_dict, state_to_dict,
)
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_event_service import StoryEventService
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.contracts.turn_journal import TurnJournalRepositoryPort
from kokoro_link.contracts.account_runtime_profile import (
    AccountRuntimeProfileResolverPort,
)
from kokoro_link.contracts.account_runtime_usage import (
    ACCOUNT_RUNTIME_EVENT_CHAT_IMAGE,
    AccountRuntimeUsageRepositoryPort,
)
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.busy_reply_decider import (
    BusyDecision,
    BusyReplyDeciderPort,
)
from kokoro_link.contracts.behavioral_pattern import BehavioralPatternRepositoryPort
from kokoro_link.contracts.dialogue_summarizer import DialogueSummarizerPort
from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.contracts.memory import WorldScope
from kokoro_link.contracts.pending_follow_up import (
    PendingFollowUpRepositoryPort,
)
from kokoro_link.contracts.character_encounter_intent import (
    CharacterEncounterIntentRepositoryPort,
)
from kokoro_link.contracts.persona_curiosity import (
    PersonaCuriosityPlan,
    PersonaCuriosityPlannerPort,
)
from kokoro_link.contracts.novelty_gate import (
    NoveltyGateContext,
    NoveltyGatePort,
    NoveltyVerdict,
)
from kokoro_link.contracts.register_profile import (
    RegisterProfile,
    RegisterProfileContext,
    RegisterProfilePort,
)
from kokoro_link.contracts.prompt_material_digest import (
    PromptMaterialDigest,
    PromptMaterialDigestContext,
    PromptMaterialDigestPort,
)
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.contracts.self_reflection import (
    SelfReflectionRepositoryPort,
)
from kokoro_link.contracts.operator_address_preference import (
    OperatorAddressPreferenceRepositoryPort,
)
from kokoro_link.contracts.address_change_log import (
    AddressChangeLogRepositoryPort,
)
from kokoro_link.application.services.relationship_names_service import (
    RelationshipNamesService,
)
from kokoro_link.application.services.experiment_overlay_service import (
    ExperimentOverlayService,
)
from kokoro_link.domain.entities.pending_follow_up import (
    MAX_QUEUED_MESSAGES,
    PendingFollowUp,
    PendingFollowUpKind,
    PendingFollowUpMessage,
)
from kokoro_link.domain.entities.turn_journal import TurnJournal
from kokoro_link.contracts.embedder import EmbedderError, EmbedderPort
from kokoro_link.contracts.feed import FeedPostRepositoryPort
from kokoro_link.contracts.goal_reviewer import GoalReviewerPort
from kokoro_link.contracts.idle_drift import IdleDrift, IdleDriftPort
from kokoro_link.contracts.llm import (
    ChatModelPort,
    ChatModelRegistryPort,
    ImageInputRejectedError,
)
from kokoro_link.contracts.nsfw_safe_summary import NsfwSafeSummaryPort
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.application.services.emotion_aggregator import (
    ExponentialDecayEmotionAggregator,
)
from kokoro_link.application.services.emotion_state_projection import (
    project_state_from_emotion_events,
)
from kokoro_link.application.services.busy_defer_policy import (
    BUSY_REPLY_DECIDER_INVOKE_FLOOR,
)
from kokoro_link.contracts.emotion import (
    EmotionAggregatorPort,
    EmotionEventRepositoryPort,
)
from kokoro_link.contracts.observability import (
    TurnRecorderPort,
    TurnRecordingDraft,
)
from kokoro_link.contracts.generation_usage import (
    UsageEventDraft,
    UsageEventRecorderPort,
)
from kokoro_link.domain.entities.generation_usage import (
    CAPABILITY_LLM,
    STATUS_FAILED,
    STATUS_SUCCEEDED,
    UsageQuantity,
)
from kokoro_link.domain.entities.emotion_event import (
    CAUSE_IDLE_DRIFT,
    CAUSE_REST_RECOVERY,
    CAUSE_TURN,
    EmotionEvent,
)
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.object_storage import ObjectStoragePort
from kokoro_link.contracts.post_turn import PostTurnProcessorPort, StateSuggestion
from kokoro_link.contracts.proactive import ProactiveAttemptRepositoryPort
from kokoro_link.contracts.self_repetition import SelfRepetitionExtractorPort
from kokoro_link.contracts.prompt import (
    PromptContextBuilderPort,
    PromptToolDescriptor,
    ToolOutcomeMessage,
)
from kokoro_link.contracts.repositories import CharacterRepositoryPort, ConversationRepositoryPort
from kokoro_link.contracts.state import StateEnginePort
from kokoro_link.contracts.tool import ToolRegistryPort
from kokoro_link.domain.entities.behavioral_pattern import KIND_PHRASE_HABIT
from kokoro_link.domain.entities.character import (
    CHAT_THAWABLE_FREEZE_REASONS,
    FREEZE_REASON_SUBSCRIPTION_LAPSE,
    Character,
)
from kokoro_link.domain.entities.character_goal import CharacterGoal
from kokoro_link.domain.entities.feed_post import FeedPost
from kokoro_link.domain.entities.schedule import ScheduleActivity
from kokoro_link.domain.entities.state_snapshot import (
    SOURCE_HEURISTIC,
    SOURCE_LLM_REFINEMENT,
    SOURCE_REST_RECOVERY,
)
from kokoro_link.domain.entities.conversation import (
    Conversation,
    Message,
    MessageAttachment,
    MessageContentMode,
    MessageKind,
    MessageRole,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    DEFAULT_PRIMARY_LANGUAGE,
    OperatorProfile,
)
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.entities.story_arc import StoryArc, StoryArcBeat
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.value_objects.character_state import CharacterState
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_COMMUNITY,
    CONTENT_TOLERANCE_FRONTIER,
    contains_restricted_messages,
    content_tolerance_for_llm_provider,
    normalize_content_tolerance,
    sanitize_messages_for_tolerance,
)
from kokoro_link.domain.value_objects.presence_frame import PresenceFrame
from kokoro_link.domain.value_objects.timezone import timezone_for_id, to_timezone
from kokoro_link.domain.value_objects.tool_call import ToolAttachment, ToolCall
from kokoro_link.infrastructure.localization import localized_fallback_text
from kokoro_link.infrastructure.memory.deduplicator import deduplicate
from kokoro_link.infrastructure.memory.ranker import rank, rank_hybrid
from kokoro_link.infrastructure.prompt.initial_relationship import (
    render_initial_relationship_seed_lines,
)
from kokoro_link.infrastructure.prompt.address_change import (
    render_address_change_lines,
)
from kokoro_link.domain.services.address_resolver import (
    resolve_character_address,
    resolve_player_address,
)
from kokoro_link.domain.value_objects.address_change_event import (
    DIRECTION_CHARACTER,
    DIRECTION_PLAYER,
    SOURCE_OBSERVED,
)
from kokoro_link.domain.value_objects.resolved_address import ResolvedAddress
from kokoro_link.infrastructure.diversity.reply_evidence import (
    build_reply_diversity_evidence,
)
from kokoro_link.infrastructure.observability.llm_metadata_wrapper import (
    LLMCallMetadata,
    MetadataCapturingChatModel,
)
from kokoro_link.infrastructure.usage.llm_metering import MeteredChatModel
from kokoro_link.infrastructure.state.recovery import apply_rest_recovery

_MEMORY_POOL_SIZE = 80
_MEMORY_PROMPT_TOP_K = 8
_RECENT_MESSAGE_LIMIT = 8
_PROMPT_RAW_RECENT_MESSAGE_LIMIT = 3
_DEFAULT_GOAL_REVIEW_INTERVAL = 10  # user-assistant exchanges between reviews
_BUSY_DECIDER_INVOKE_FLOOR = BUSY_REPLY_DECIDER_INVOKE_FLOOR
"""Skip the busy-reply decider's LLM call when the current activity's
``busy_score`` is below this threshold.

This is a **perf gate, not a behavior gate** — the decider remains the
sole authority on whether to defer. The floor just stops us from
spending an LLM call on patently idle states (a character lounging at
``busy_score=0.2`` is not going to defer no matter what the model says).
``CLAUDE.md`` forbids using thresholds / keywords to **decide** the
outcome; using them to **gate the LLM invocation** for cost reasons is
fine and is the same pattern used by the idle-drift judge."""

_DEFAULT_IDLE_DRIFT_THRESHOLD_MINUTES = 120.0
"""Below this idle gap we skip the idle-drift LLM call entirely.

Two hours is the rough boundary between "stepped away from the chat"
and "an actual absence the character would feel". Going lower spams
the LLM during normal back-and-forth (lunch break, meeting, sleep
after a late-night chat). Going higher misses overnight gaps that
clearly should colour the morning's first reply. Tunable per
deployment via constructor."""
_DEFAULT_SELF_REPETITION_INTERVAL = 5
"""Default chat turns between self-repetition extractor runs.

Smaller than goal-review (every 10) because phrasing habits form
faster than goal progress; large enough that one cheap LLM call
amortises over ~5 turns of chat. Operators can override per character
via constructor."""
_SELF_REPETITION_WINDOW = 10
"""How many of the character's most recent assistant turns the
extractor reads. Larger than the prompt's literal-lines rail (which
shows the last 3) so the extractor can spot patterns that only
emerge after several turns."""
_VISION_HISTORY_LIMIT = 2
"""How many image attachments to carry into the prompt across history.

Vision tokens are expensive (~600-1500 per 768² image on cloud
providers, plus context-window pressure on local models), so we cap
the count rather than the turn count. Newest-first FIFO: when we go
over budget we drop the oldest images first. Set to 0 to disable
history vision entirely."""
_IMAGE_RECOGNITION_CONTEXT_MAX_CHARS = 4000
"""Maximum image-recognition text injected into the main chat prompt."""
_MAX_TOOL_HOPS = 4
"""Cap on tool-call iterations per turn.

Each hop = one ``model.generate``; tool-emitting hops also run one
tool. Budget of 4 lets the model chain up to three tool calls
(e.g. ``web_search`` → ``web_fetch`` → ``generate_image``) and still
land a final user-facing reply. The final hop always hides the tool
block so the loop terminates with text even if the model would
otherwise keep calling tools."""
_LOGGER = logging.getLogger(__name__)


class ChatRuntimeLimitExceeded(RuntimeError):
    """Raised when an account runtime profile denies the next chat turn."""


class ChatSubscriptionFrozen(RuntimeError):
    """Legacy fallback for pre-tenant-state subscription freeze rows.

    Current authorization is enforced by ``SubscriptionAccessGuard`` before
    character thawing. This exception keeps unresolved legacy
    ``frozen_reason='subscription_lapse'`` rows fail-closed during upgrades.
    The API layer maps both paths to the same structured 403 response."""


def _compute_idle_minutes(state: CharacterState, now: datetime) -> float | None:
    """Minutes since the user last sent a message, or ``None`` if unknown.

    Uses ``CharacterState.last_active_at`` which is written at the end
    of each turn — so when a new turn is being built, it still points
    at the *previous* interaction. Clamped at zero to defend against
    clock skew.
    """
    last = state.last_active_at
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return max(0.0, (now - last).total_seconds() / 60.0)


@dataclass(frozen=True, slots=True)
class ChatGenerationTrace:
    """Recorded facts for the model call that produced the chat reply."""

    prompt_assembled: str = ""
    prompt_pack_hash: str = ""
    model_id: str = ""
    latency_ms: int | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    error: str | None = None


@dataclass(frozen=True, slots=True)
class ChatGenerationResult:
    text: str
    attachments: list[MessageAttachment]
    forced_fired: bool
    trace: ChatGenerationTrace
    persona_curiosity_plan: PersonaCuriosityPlan | None = None
    material_digest: PromptMaterialDigest | None = None
    register_profile: RegisterProfile | None = None
    diversity_evidence: ReplyDiversityEvidence | None = None
    novelty_verdict: NoveltyVerdict | None = None
    novelty_retry_count: int = 0


def _image_drop_placeholder(count: int) -> str:
    """Text appended to the prompt when ``count`` images are dropped.

    Single source of truth for the "you can't see these images" hint,
    shared by ``_prepare_vision_prompt`` (proactive downgrade when the
    model isn't vision-capable / URLs can't be resolved) and the
    ImageInputRejectedError degrade retry in ``_generate_capturing`` /
    ``_stream_capturing`` (reactive downgrade when the upstream rejects
    the image parts)."""
    return (
        f"\n\n[使用者這則訊息另外附帶了 {count} 張圖片；"
        "目前模型不支援視覺或無法存取，你沒看到圖片內容。]"
    )


def _log_image_rejection_degrade(
    model,
    *,
    model_id: str | None,
    error: ImageInputRejectedError,
) -> None:
    """One WARNING per degrade so operators can find a mis-set vision
    flag pointed at a text-only upstream."""
    _LOGGER.warning(
        "upstream rejected image input (status=%s) for provider=%s model=%s; "
        "degrading turn — retrying once without images",
        error.status_code,
        str(getattr(model, "provider_id", "") or ""),
        model_id or "",
    )


async def _generate_capturing(
    model,
    prompt: str,
    *,
    image_urls: tuple[str, ...] = (),
    model_id: str | None = None,
) -> tuple[str, ChatGenerationTrace]:
    """Call a chat model and return replay-ready metadata.

    Existing adapters still expose the narrow ``ChatModelPort`` API, so
    the metadata wrapper is applied locally at the call site. This keeps
    replay capture opt-in and avoids widening every provider contract.

    If the upstream rejects the image parts (``ImageInputRejectedError``)
    and images were actually sent, the turn degrades: retry exactly once
    without images, with the drop-placeholder appended to the prompt.
    """
    wrapper = (
        model
        if hasattr(model, "generate_capturing")
        else MetadataCapturingChatModel(model)
    )
    sent_prompt = prompt
    try:
        captured = await wrapper.generate_capturing(
            prompt,
            image_urls=image_urls,
            model=model_id,
        )
    except ImageInputRejectedError as exc:
        if not image_urls:
            raise
        _log_image_rejection_degrade(model, model_id=model_id, error=exc)
        sent_prompt = prompt + _image_drop_placeholder(len(image_urls))
        captured = await wrapper.generate_capturing(
            sent_prompt,
            image_urls=(),
            model=model_id,
        )
    return captured.text, _trace_from_metadata(
        prompt=sent_prompt,
        metadata=captured.metadata,
        fallback_model_id=model_id,
    )


async def _stream_capturing(
    model,
    prompt: str,
    *,
    image_urls: tuple[str, ...] = (),
    model_id: str | None = None,
) -> tuple[AsyncIterator[str], object, str]:
    """Token-streaming counterpart to ``_generate_capturing``.

    An ``ImageInputRejectedError`` on the streaming path surfaces when
    the first chunk is pulled (the upstream 4xx lands before any token).
    We pull that first chunk here, before returning, so a degrade retry
    (no images, drop-placeholder prompt) can be swapped in transparently
    — the SSE consumer never sees a partial-then-crash. The returned
    ``capture`` is always the one that actually produced the stream (the
    retry's, when a degrade happened), preserving the metadata contract.

    Returns ``(stream, capture, sent_prompt)``. ``sent_prompt`` is the
    prompt that was ACTUALLY sent upstream — the original, or original +
    drop placeholder after a degrade retry — so trace construction
    records what the model really saw, not the pre-degrade prompt.
    """
    wrapper = (
        model
        if hasattr(model, "generate_stream_capturing")
        else MetadataCapturingChatModel(model)
    )

    async def _open(call_prompt: str, call_image_urls: tuple[str, ...]):
        ctx = wrapper.generate_stream_capturing(
            call_prompt,
            image_urls=call_image_urls,
            model=model_id,
        )
        capture = await ctx.__aenter__()
        return ctx, capture, capture.chunks()

    ctx, capture, chunk_iter = await _open(prompt, image_urls)
    sent_prompt = prompt
    have_first = False
    first_chunk = ""
    try:
        first_chunk = await chunk_iter.__anext__()
        have_first = True
    except StopAsyncIteration:
        pass  # empty stream — nothing to buffer
    except ImageInputRejectedError as exc:
        await ctx.__aexit__(type(exc), exc, exc.__traceback__)
        if not image_urls:
            raise
        _log_image_rejection_degrade(model, model_id=model_id, error=exc)
        retry_prompt = prompt + _image_drop_placeholder(len(image_urls))
        ctx, capture, chunk_iter = await _open(retry_prompt, ())
        sent_prompt = retry_prompt
        try:
            first_chunk = await chunk_iter.__anext__()
            have_first = True
        except StopAsyncIteration:
            pass
        except BaseException as retry_exc:
            await ctx.__aexit__(
                type(retry_exc), retry_exc, retry_exc.__traceback__,
            )
            raise
    except BaseException as exc:
        # Any other first-pull failure (5xx, connect error, plain
        # HTTPStatusError…) is not ours to reclassify — close the
        # stream context so it can't leak, then propagate unchanged.
        await ctx.__aexit__(type(exc), exc, exc.__traceback__)
        raise

    async def _chunks(
        active_ctx, active_iter, *, buffered: str, has_buffered: bool,
    ) -> AsyncIterator[str]:
        try:
            if has_buffered:
                yield buffered
            async for chunk in active_iter:
                yield chunk
        except BaseException as exc:
            await active_ctx.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        else:
            await active_ctx.__aexit__(None, None, None)

    return (
        _chunks(ctx, chunk_iter, buffered=first_chunk, has_buffered=have_first),
        capture,
        sent_prompt,
    )


def _trace_from_metadata(
    *,
    prompt: str,
    metadata: LLMCallMetadata | None,
    fallback_model_id: str | None = None,
    prompt_pack_hash: str = "",
) -> ChatGenerationTrace:
    if metadata is None:
        return ChatGenerationTrace(
            prompt_assembled=prompt,
            prompt_pack_hash=prompt_pack_hash,
            model_id=fallback_model_id or "",
        )
    return ChatGenerationTrace(
        prompt_assembled=prompt,
        prompt_pack_hash=prompt_pack_hash,
        model_id=metadata.model_id or fallback_model_id or "",
        latency_ms=metadata.latency_ms,
        prompt_tokens=metadata.prompt_tokens,
        completion_tokens=metadata.completion_tokens,
        error=metadata.error,
    )


def _failed_generation_trace(
    *,
    prompt: str,
    prompt_pack_hash: str,
    model_id: str | None,
    error: BaseException,
) -> ChatGenerationTrace:
    return ChatGenerationTrace(
        prompt_assembled=prompt,
        prompt_pack_hash=prompt_pack_hash,
        model_id=model_id or "",
        error=repr(error),
    )


def _merge_traces(traces: list[ChatGenerationTrace]) -> ChatGenerationTrace:
    """Fold multi-hop tool calls into one replay row.

    ``prompt_assembled`` is the prompt that produced the final user-facing
    response; latency/tokens are summed across hops because the operator
    cares about total turn cost in the dashboard.
    """
    if not traces:
        return ChatGenerationTrace()
    final = traces[-1]
    latency = sum(t.latency_ms or 0 for t in traces)
    prompt_tokens = sum(t.prompt_tokens or 0 for t in traces)
    completion_tokens = sum(t.completion_tokens or 0 for t in traces)
    return ChatGenerationTrace(
        prompt_assembled=final.prompt_assembled,
        prompt_pack_hash=final.prompt_pack_hash,
        model_id=final.model_id,
        latency_ms=latency if any(t.latency_ms is not None for t in traces) else None,
        prompt_tokens=(
            prompt_tokens if any(t.prompt_tokens is not None for t in traces)
            else None
        ),
        completion_tokens=(
            completion_tokens if any(t.completion_tokens is not None for t in traces)
            else None
        ),
        error=final.error,
    )


def _last_prompt_pack_hash(prompt_context_builder: object) -> str:
    return str(getattr(prompt_context_builder, "last_prompt_pack_hash", "") or "")


class ChatService:
    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        conversation_repository: ConversationRepositoryPort,
        memory_repository: MemoryRepositoryPort,
        post_turn_processor: PostTurnProcessorPort,
        prompt_context_builder: PromptContextBuilderPort,
        model_registry: ChatModelRegistryPort,
        state_engine: StateEnginePort,
        active_llm_provider: ActiveLLMProviderPort | None = None,
        nsfw_mode_service: NsfwModeService | None = None,
        goal_service: GoalService | None = None,
        goal_reviewer: GoalReviewerPort | None = None,
        goal_review_interval: int = _DEFAULT_GOAL_REVIEW_INTERVAL,
        self_repetition_extractor: "SelfRepetitionExtractorPort | None" = None,
        self_repetition_interval: int = _DEFAULT_SELF_REPETITION_INTERVAL,
        behavioral_pattern_repository: BehavioralPatternRepositoryPort | None = None,
        schedule_service: ScheduleService | None = None,
        schedule_memorializer: ScheduleMemorializer | None = None,
        feed_reaction_memorializer: FeedReactionMemorializer | None = None,
        dialogue_summarizer: DialogueSummarizerPort | None = None,
        embedder: EmbedderPort | None = None,
        state_tracker: "StateChangeTracker | None" = None,
        auto_consolidation_trigger: AutoConsolidationTrigger | None = None,
        tool_registry: ToolRegistryPort | None = None,
        tool_orchestrator: ToolOrchestrator | None = None,
        story_event_service: StoryEventService | None = None,
        story_arc_service: "StoryArcService | None" = None,
        proactive_attempt_repository: ProactiveAttemptRepositoryPort | None = None,
        feed_post_repository: FeedPostRepositoryPort | None = None,
        journal_repository: TurnJournalRepositoryPort | None = None,
        journal_keep_per_conversation: int = 5,
        extract_in_background: bool = False,
        public_base_url: str = "",
        uploads_dir: Path | None = None,
        object_storage: ObjectStoragePort | None = None,
        operator_profile_service: "OperatorProfileService | None" = None,
        tts_pregenerator: "TTSPregenerationService | None" = None,
        idle_drift_judge: IdleDriftPort | None = None,
        idle_drift_threshold_minutes: float = _DEFAULT_IDLE_DRIFT_THRESHOLD_MINUTES,
        busy_reply_decider: "BusyReplyDeciderPort | None" = None,
        pending_follow_up_repository: "PendingFollowUpRepositoryPort | None" = None,
        character_encounter_intent_repository: (
            CharacterEncounterIntentRepositoryPort | None
        ) = None,
        persona_extraction_service: "PersonaExtractionService | None" = None,
        operator_persona_service: "OperatorPersonaService | None" = None,
        character_social_knowledge_service: (
            "CharacterSocialKnowledgeService | None"
        ) = None,
        relationship_seed_repository: (
            CharacterOperatorRelationshipSeedRepositoryPort | None
        ) = None,
        persona_curiosity_service: "PersonaCuriosityService | None" = None,
        persona_curiosity_planner: "PersonaCuriosityPlannerPort | None" = None,
        prompt_material_digester: PromptMaterialDigestPort | None = None,
        prompt_material_digest_enabled: bool = False,
        register_profiler: RegisterProfilePort | None = None,
        register_profile_enabled: bool = False,
        novelty_gate: NoveltyGatePort | None = None,
        novelty_gate_enabled: bool = False,
        novelty_gate_max_retries: int = 1,
        reply_quality_gate_risk_threshold: float = 0.0,
        reply_quality_similarity_threshold: float = 0.88,
        turn_recorder: TurnRecorderPort | None = None,
        usage_recorder: UsageEventRecorderPort | None = None,
        emotion_event_repository: EmotionEventRepositoryPort | None = None,
        emotion_aggregator: EmotionAggregatorPort | None = None,
        self_reflection_repository: "SelfReflectionRepositoryPort | None" = None,
        address_preference_repository: "OperatorAddressPreferenceRepositoryPort | None" = None,
        address_change_log_repository: "AddressChangeLogRepositoryPort | None" = None,
        relationship_names_service: "RelationshipNamesService | None" = None,
        experiment_overlay_service: "ExperimentOverlayService | None" = None,
        nsfw_safe_summarizer: NsfwSafeSummaryPort | None = None,
        account_runtime_profile_resolver: (
            AccountRuntimeProfileResolverPort | None
        ) = None,
        account_runtime_usage_repository: (
            AccountRuntimeUsageRepositoryPort | None
        ) = None,
        event_seed_dispenser=None,  # noqa: ANN001 - optional EventSeedDispenser
        clock: ClockPort | None = None,
        subscription_access_guard: SubscriptionAccessGuard | None = None,
    ) -> None:
        self._character_repository = character_repository
        self._conversation_repository = conversation_repository
        self._memory_repository = memory_repository
        self._post_turn_processor = post_turn_processor
        self._prompt_context_builder = prompt_context_builder
        self._model_registry = model_registry
        self._active_llm_provider = active_llm_provider
        self._nsfw_mode_service = nsfw_mode_service
        self._state_engine = state_engine
        self._goal_service = goal_service
        self._goal_reviewer = goal_reviewer
        self._goal_review_interval = max(1, goal_review_interval)
        self._self_repetition_extractor = self_repetition_extractor
        self._self_repetition_interval = max(1, self_repetition_interval)
        self._behavioral_patterns = behavioral_pattern_repository
        # Per-conversation hint cache: ``conversation_id ->
        # (turn_index_when_extracted, hint_text)``. Lost on restart and
        # not shared across backend instances — both acceptable because
        # the extractor reruns within ``_self_repetition_interval``
        # turns and the chat path treats a missing hint as "no rail".
        self._self_repetition_cache: dict[str, tuple[int, str]] = {}
        self._schedule_service = schedule_service
        self._schedule_memorializer = schedule_memorializer
        self._feed_reaction_memorializer = feed_reaction_memorializer
        self._dialogue_summarizer = dialogue_summarizer
        self._embedder = embedder
        self._state_tracker = state_tracker
        self._auto_consolidation_trigger = auto_consolidation_trigger
        self._tool_registry = tool_registry
        self._tool_orchestrator = tool_orchestrator
        self._story_event_service = story_event_service
        self._story_arc_service = story_arc_service
        self._proactive_attempt_repository = proactive_attempt_repository
        self._feed_post_repository = feed_post_repository
        self._journal_repository = journal_repository
        self._journal_keep = max(1, journal_keep_per_conversation)
        self._extract_in_background = extract_in_background
        self._public_base_url = (public_base_url or "").rstrip("/")
        _ = uploads_dir
        self._uploads_dir = None
        self._object_storage = object_storage
        self._operator_profile_service = operator_profile_service
        self._tts_pregenerator = tts_pregenerator
        self._idle_drift_judge = idle_drift_judge
        self._idle_drift_threshold_minutes = max(0.0, idle_drift_threshold_minutes)
        self._busy_reply_decider = busy_reply_decider
        self._pending_follow_up_repository = pending_follow_up_repository
        self._character_encounter_intent_repository = (
            character_encounter_intent_repository
        )
        self._persona_extraction_service = persona_extraction_service
        self._operator_persona_service = operator_persona_service
        self._character_social_knowledge_service = character_social_knowledge_service
        self._relationship_seed_repository = relationship_seed_repository
        self._persona_curiosity_service = persona_curiosity_service
        self._persona_curiosity_planner = persona_curiosity_planner
        self._prompt_material_digester = prompt_material_digester
        self._prompt_material_digest_enabled = bool(prompt_material_digest_enabled)
        self._register_profiler = register_profiler
        self._register_profile_enabled = bool(register_profile_enabled)
        self._novelty_gate = novelty_gate
        self._novelty_gate_enabled = bool(novelty_gate_enabled)
        self._novelty_gate_max_retries = max(0, int(novelty_gate_max_retries))
        self._reply_quality_gate_risk_threshold = max(
            0.0,
            min(1.0, float(reply_quality_gate_risk_threshold)),
        )
        self._reply_quality_similarity_threshold = max(
            0.0,
            min(1.0, float(reply_quality_similarity_threshold)),
        )
        self._turn_recorder = turn_recorder
        self._usage_recorder = usage_recorder
        self._emotion_event_repository = emotion_event_repository
        self._emotion_aggregator: EmotionAggregatorPort = (
            emotion_aggregator or ExponentialDecayEmotionAggregator()
        )
        self._self_reflection_repository = self_reflection_repository
        self._address_preference_repository = address_preference_repository
        self._address_change_log_repository = address_change_log_repository
        self._relationship_names_service = relationship_names_service
        self._experiment_overlay_service = experiment_overlay_service
        self._nsfw_safe_summarizer = nsfw_safe_summarizer
        self._account_runtime_profile_resolver = (
            account_runtime_profile_resolver
            or PermissiveAccountRuntimeProfileResolver()
        )
        self._account_runtime_usage_repository = account_runtime_usage_repository
        self._event_seed_dispenser = event_seed_dispenser
        self._clock = clock
        self._subscription_access_guard = subscription_access_guard
        self._pending_tasks: set[asyncio.Task] = set()

    def _resolve_now(self, now: datetime | None = None) -> datetime:
        return ensure_utc(
            now if now is not None else (
                self._clock.now()
                if self._clock is not None
                else datetime.now(timezone.utc)
            ),
        )

    async def _content_mode_for_character(self, character: Character) -> MessageContentMode:
        if self._nsfw_mode_service is None:
            return MessageContentMode.NORMAL
        mode = await self._nsfw_mode_service.content_mode_for_write(
            user_id=character.user_id,
        )
        if mode == CONTENT_MODE_NSFW:
            await self._nsfw_mode_service.refresh_activity(
                user_id=character.user_id,
            )
            return MessageContentMode.NSFW
        return MessageContentMode.NORMAL

    async def _with_nsfw_safe_summaries(
        self,
        *,
        character: Character,
        conversation_id: str | None = None,
        user_message: Message,
        assistant_message: Message,
        model: ChatModelPort | None = None,
        model_id: str | None = None,
    ) -> tuple[Message, Message]:
        summarizer = self._nsfw_safe_summarizer
        if summarizer is None:
            return user_message, assistant_message
        if (
            user_message.content_mode is not MessageContentMode.NSFW
            and assistant_message.content_mode is not MessageContentMode.NSFW
        ):
            return user_message, assistant_message

        metered_model = self._meter_nsfw_safe_summary_model(
            model,
            character=character,
            conversation_id=conversation_id,
        )

        async def summarize(message: Message) -> Message:
            if message.content_mode is not MessageContentMode.NSFW:
                return message
            summary = await summarizer.summarize(
                character=character,
                message=message,
                model=metered_model,
                model_id=model_id,
            )
            if not summary:
                return message
            return replace(message, safe_summary=summary)

        safe_user, safe_assistant = await asyncio.gather(
            summarize(user_message),
            summarize(assistant_message),
        )
        return safe_user, safe_assistant

    def _meter_nsfw_safe_summary_model(
        self,
        model: ChatModelPort | None,
        *,
        character: Character,
        conversation_id: str | None,
    ) -> ChatModelPort | None:
        if model is None or self._usage_recorder is None:
            return model
        return MeteredChatModel(
            inner=model,
            recorder=lambda: self._usage_recorder,
            feature_key=FEATURE_NSFW_SAFE_SUMMARY,
            character=character,
            operator_id=character.user_id,
            content_tolerance=_content_tolerance_for_model(
                model,
                content_mode=MessageContentMode.NSFW,
            ),
            source_surface=FEATURE_NSFW_SAFE_SUMMARY,
            routing_mode="chat_safe_summary",
            conversation_id=conversation_id,
            metered_by="chat_service",
        )

    def _schedule_nsfw_safe_summary_generation(
        self,
        *,
        character: Character,
        conversation_id: str,
        user_position: int,
        assistant_position: int,
        content_mode: MessageContentMode,
        model: ChatModelPort | None,
        model_id: str | None,
    ) -> None:
        if (
            content_mode is not MessageContentMode.NSFW
            or self._nsfw_safe_summarizer is None
            or model is None
        ):
            return
        self._schedule_background(
            self._write_nsfw_safe_summaries(
                character=character,
                conversation_id=conversation_id,
                user_position=user_position,
                assistant_position=assistant_position,
                model=model,
                model_id=model_id,
            ),
        )

    async def _write_nsfw_safe_summaries(
        self,
        *,
        character: Character,
        conversation_id: str,
        user_position: int,
        assistant_position: int,
        model: ChatModelPort,
        model_id: str | None,
    ) -> None:
        conversation = await self._conversation_repository.get(conversation_id)
        if conversation is None:
            return
        if (
            user_position < 0
            or assistant_position < 0
            or user_position >= len(conversation.messages)
            or assistant_position >= len(conversation.messages)
        ):
            return
        user_message = conversation.messages[user_position]
        assistant_message = conversation.messages[assistant_position]
        if (
            user_message.role is not MessageRole.USER
            or assistant_message.role is not MessageRole.ASSISTANT
        ):
            return
        safe_user, safe_assistant = await self._with_nsfw_safe_summaries(
            character=character,
            conversation_id=conversation_id,
            user_message=user_message,
            assistant_message=assistant_message,
            model=model,
            model_id=model_id,
        )
        if (
            safe_user.safe_summary == user_message.safe_summary
            and safe_assistant.safe_summary == assistant_message.safe_summary
        ):
            return
        await self._conversation_repository.save(
            _replace_messages_at_positions(
                conversation,
                replacements={
                    user_position: safe_user,
                    assistant_position: safe_assistant,
                },
            ),
        )

    async def _is_nsfw_active_for_character(self, character: Character) -> bool:
        if self._nsfw_mode_service is None:
            return False
        return (
            await self._nsfw_mode_service.active_target(user_id=character.user_id)
            is not None
        )

    async def _resolve_main_chat_model(
        self,
        *,
        character: Character,
        payload: SendChatMessageRequest,
    ) -> tuple[ChatModelPort, str | None]:
        """Resolve the model for the player-visible chat reply.

        Normal player requests do not carry provider/model anymore; they
        follow admin/global ``FEATURE_CHAT`` routing. Explicit request
        provider/model remains as a legacy/debug override path.
        """
        nsfw_active = await self._is_nsfw_active_for_character(character)
        if payload.provider_id and not nsfw_active:
            provider_id, model_id = _resolve_chat_provider_and_model(
                character=character,
                payload=payload,
            )
            return self._model_registry.resolve(provider_id), model_id

        if self._active_llm_provider is not None:
            try:
                model = await self._active_llm_provider.resolve(
                    FEATURE_CHAT,
                    character=character,
                )
                resolved_model_id = await (
                    self._active_llm_provider.resolve_model_id(
                        FEATURE_CHAT,
                        character=character,
                    )
                )
                model_id = resolved_model_id if nsfw_active else (
                    payload.model_id or resolved_model_id
                )
                return model, model_id
            except Exception:
                _LOGGER.exception(
                    "chat model active-provider resolution failed; "
                    "falling back to registry default",
                )

        provider_id, model_id = _resolve_chat_provider_and_model(
            character=character,
            payload=payload,
        )
        return self._model_registry.resolve(provider_id), model_id

    async def _build_image_recognition_context(
        self,
        *,
        character: Character,
        main_model,
        attachment_urls,
        content_tolerance: str,
    ) -> str:
        urls = [u for u in (attachment_urls or ()) if u]
        if not urls:
            return ""
        if bool(getattr(main_model, "supports_vision", False)):
            return ""
        if self._active_llm_provider is None:
            return ""

        try:
            recognition_model = await self._active_llm_provider.resolve(
                FEATURE_IMAGE_RECOGNITION,
                character=character,
                content_tolerance=content_tolerance,
            )
        except Exception:
            _LOGGER.exception(
                "image-recognition model resolution failed; "
                "falling back to text hint",
            )
            return ""

        if not bool(getattr(recognition_model, "supports_vision", False)):
            _LOGGER.info(
                "image-recognition route resolved to a non-vision model; "
                "falling back to text hint",
            )
            return ""

        resolved_urls: list[str] = []
        for url in urls:
            converted = await _to_vision_url_with_storage(
                url,
                uploads_dir=self._uploads_dir,
                public_base_url=self._public_base_url,
                object_storage=self._object_storage,
            )
            if not converted:
                _LOGGER.warning(
                    "image-recognition route couldn't resolve an "
                    "attachment URL; falling back to text hint",
                )
                return ""
            resolved_urls.append(converted)

        try:
            recognition_model_id = await self._active_llm_provider.resolve_model_id(
                FEATURE_IMAGE_RECOGNITION,
                character=character,
                content_tolerance=content_tolerance,
            )
        except Exception:
            _LOGGER.exception(
                "image-recognition model-id resolution failed; "
                "using provider default",
            )
            recognition_model_id = None

        try:
            text = await recognition_model.generate(
                _image_recognition_prompt(len(resolved_urls)),
                image_urls=tuple(resolved_urls),
                model=recognition_model_id,
            )
        except Exception:
            _LOGGER.exception(
                "image-recognition generation failed; falling back to text hint",
            )
            return ""

        return _clean_image_recognition_context(text)

    async def _resolve_experiment_overlay(
        self, *, character_id: str, operator_id: str,
    ) -> dict[str, str]:
        """HUMANIZATION_ROADMAP §4.6 — flat overlay for variant routing.

        ``operator_id`` is the character owner's id (= the user
        chatting). Multi-user note: pre-auth this hard-coded
        ``DEFAULT_OPERATOR_ID`` because every character belonged to the
        same singleton; after multi-user auth callers thread the owner
        id through so experiments stay per-user.
        """
        if self._experiment_overlay_service is None:
            return {}
        try:
            return await self._experiment_overlay_service.resolve_overlay(
                character_id=character_id,
                operator_id=operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "experiment overlay lookup crashed character=%s",
                character_id,
            )
            return {}

    async def _fetch_address_preference(
        self, *, character_id: str, operator_id: str,
    ):
        """HUMANIZATION_ROADMAP §4.2 — look up observed register / address
        preference for ``operator_id``. Returns ``None`` when the
        repository is unwired or the lookup fails — the prompt builder
        treats that as "fall back to §3.6 explicit pace knob".

        Multi-user note: same migration as ``_resolve_experiment_overlay``
        — pre-auth this hard-coded the default operator id, now it
        threads the character owner id."""
        if self._address_preference_repository is None:
            return None
        try:
            return await self._address_preference_repository.get(
                character_id=character_id,
                operator_id=operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "address_preference lookup crashed character=%s",
                character_id,
            )
            return None

    async def _record_turn_safely(self, draft: TurnRecordingDraft) -> None:
        """Forward to the turn recorder, swallowing all errors.

        Recording is auxiliary — a recorder outage must never break a
        chat turn. Adapters are already fire-and-forget internally; this
        wrapper protects against the no-recorder case and any synchronous
        construction failures.
        """
        if self._turn_recorder is None:
            return
        try:
            await self._turn_recorder.record(draft)
        except Exception:  # noqa: BLE001 — auxiliary, never bubble
            _LOGGER.exception(
                "turn_recorder dispatch failed (kind=%s, character=%s)",
                draft.kind, draft.character_id,
            )

    async def _record_llm_usage_safely(
        self,
        *,
        character_id: str,
        operator_id: str,
        conversation_id: str,
        turn_record_id: str | None,
        trace: ChatGenerationTrace,
        provider_id: str,
        source_surface: str,
        upstream_request_id: str = "",
        forced_tool: bool = False,
    ) -> None:
        """Record aggregate LLM usage for one chat turn without blocking chat."""
        if self._usage_recorder is None:
            return
        prompt_tokens = trace.prompt_tokens
        completion_tokens = trace.completion_tokens
        input_quantity = int(prompt_tokens or 0)
        output_quantity = int(completion_tokens or 0)
        total_quantity = input_quantity + output_quantity
        try:
            await self._usage_recorder.record(UsageEventDraft(
                capability=CAPABILITY_LLM,
                turn_record_id=turn_record_id,
                conversation_id=conversation_id,
                character_id=character_id,
                operator_id=operator_id,
                feature_key=FEATURE_CHAT,
                source_surface=source_surface,
                upstream_request_id=upstream_request_id,
                provider_id=provider_id,
                model_id=trace.model_id or provider_id,
                prompt_pack_hash=trace.prompt_pack_hash,
                quantity=UsageQuantity(
                    usage_unit="token",
                    input_quantity=input_quantity,
                    output_quantity=output_quantity,
                    total_quantity=total_quantity,
                    billable_quantity=total_quantity,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    usage_is_estimated=True,
                ),
                latency_ms=trace.latency_ms,
                status=STATUS_FAILED if trace.error else STATUS_SUCCEEDED,
                error_code="llm_error" if trace.error else None,
                error_message=trace.error,
                metadata={"aggregate": True, "forced_tool": forced_tool},
            ))
        except Exception:  # noqa: BLE001 — auxiliary, never bubble
            _LOGGER.exception(
                "usage_recorder dispatch failed (feature=%s, character=%s)",
                FEATURE_CHAT,
                character_id,
            )

    def _overlay_emotion_on_state(
        self,
        *,
        state: CharacterState,
        events: list[EmotionEvent],
        now: datetime,
    ) -> CharacterState:
        """Project read-time state from the event stream.

        Flat ``CharacterState`` columns remain the compatibility
        baseline. New emotion events that are not already column-applied
        become the numeric source of truth for the displayed/prompted
        state; legacy/applied events only contribute labels and
        provenance so they do not double-count.
        """
        return project_state_from_emotion_events(
            state=state,
            events=events,
            aggregator=self._emotion_aggregator,
            now=now,
        )

    async def _load_recent_emotion_events(
        self,
        *,
        character_id: str,
        operator: "OperatorProfile | None",
        now: datetime,
        owner_user_id: str | None = None,
    ) -> list[EmotionEvent]:
        """Read the last 24h of EmotionEvent rows for prompt injection.

        Returns an empty list when the repository isn't wired — the
        prompt builder treats that as "no recent emotion fact section"
        and emits no block. Errors are swallowed for the same reason
        ``_record_turn_safely`` swallows them: this is an enrichment,
        not a hard requirement, and a slow read shouldn't break chat.

        Multi-user fallback: ``owner_user_id`` is the character's owner
        id — preferred over ``DEFAULT_OPERATOR_ID`` when ``operator`` is
        missing so the lookup stays scoped to the right user.
        """
        if self._emotion_event_repository is None:
            return []
        operator_id = (
            operator.id if operator is not None
            else (owner_user_id or DEFAULT_OPERATOR_ID)
        )
        since = now - timedelta(hours=24)
        try:
            return await self._emotion_event_repository.list_recent(
                character_id=character_id,
                operator_id=operator_id,
                since=since,
                limit=30,
            )
        except Exception:  # noqa: BLE001 — auxiliary, never bubble
            _LOGGER.exception(
                "emotion_event_repository.list_recent failed (character=%s)",
                character_id,
            )
            return []

    async def _record_emotion_event_candidates(
        self,
        *,
        character: Character,
        cause_ref_id: str,
        operator: "OperatorProfile | None",
        candidates: list,
    ) -> list[str]:
        """Persist LLM-emitted ``EmotionEventCandidate`` rows verbatim.

        These are the "rich" path: the LLM already supplied evidence
        quote, valence, arousal, and a tailored half-life per event.
        Skipping the ``state_suggestion`` mirror avoids double-counting
        — when candidates exist they replace the rough delta mirror.
        """
        if self._emotion_event_repository is None or not candidates:
            return []
        operator_id = (
            operator.id if operator is not None
            else getattr(character, "user_id", DEFAULT_OPERATOR_ID)
        )
        events: list[EmotionEvent] = []
        for c in candidates:
            events.append(EmotionEvent.new(
                character_id=character.id,
                operator_id=operator_id,
                cause_ref_kind=CAUSE_TURN,
                cause_ref_id=cause_ref_id,
                valence=c.valence,
                arousal=c.arousal,
                intensity=c.intensity,
                affection_delta=c.affection_delta,
                fatigue_delta=c.fatigue_delta,
                trust_delta=c.trust_delta,
                energy_delta=c.energy_delta,
                applied_to_state=False,
                emotion_label=c.emotion_label,
                evidence_quote=c.evidence_quote,
                decay_half_life_minutes=c.decay_half_life_minutes,
            ))
        try:
            await self._emotion_event_repository.add_many(events)
            return [event.id for event in events]
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "emotion_event_repository.add_many failed (character=%s, n=%d)",
                character.id, len(events),
            )
            return []

    async def _record_emotion_event_from_state_suggestion(
        self,
        *,
        character: Character,
        cause_ref_id: str,
        operator: "OperatorProfile | None",
        suggestion: "StateSuggestion",
        assistant_text: str,
    ) -> list[str]:
        """Mirror a ``StateSuggestion`` onto an ``EmotionEvent`` row.

        Additive: doesn't replace the existing flat-column state path.
        Once the derived-view migration lands the aggregator will become
        authoritative; until then this is the data feed for the
        observability dashboard + prompt-side 24h summary.
        """
        if self._emotion_event_repository is None:
            return []
        # Skip totally empty suggestions — happens on light "ack" turns
        # where the LLM had nothing to say.
        has_signal = (
            (suggestion.emotion or "")
            or suggestion.affection_delta
            or suggestion.fatigue_delta
            or suggestion.trust_delta
            or suggestion.energy_delta
        )
        if not has_signal:
            return []
        operator_id = (
            operator.id if operator is not None
            else getattr(character, "user_id", DEFAULT_OPERATOR_ID)
        )
        intensity = _estimate_intensity_from_deltas(suggestion)
        try:
            event = EmotionEvent.new(
                character_id=character.id,
                operator_id=operator_id,
                cause_ref_kind=CAUSE_TURN,
                cause_ref_id=cause_ref_id,
                affection_delta=int(suggestion.affection_delta),
                fatigue_delta=int(suggestion.fatigue_delta),
                trust_delta=int(suggestion.trust_delta),
                energy_delta=int(suggestion.energy_delta),
                applied_to_state=False,
                emotion_label=(suggestion.emotion or "").strip(),
                evidence_quote=assistant_text[:120],
                intensity=intensity,
                # Default 4-hour half-life matches the existing
                # rest-recovery decay; idle drift / dream events override
                # when they get their own emission path.
                decay_half_life_minutes=240,
            )
            await self._emotion_event_repository.add(event)
            return [event.id]
        except Exception:  # noqa: BLE001 — auxiliary, never bubble
            _LOGGER.exception(
                "emotion_event_repository.add failed (character=%s)",
                character.id,
            )
            return []

    def set_tts_pregenerator(
        self,
        pregenerator: "TTSPregenerationService | None",
    ) -> None:
        self._tts_pregenerator = pregenerator

    async def send_message(
        self,
        payload: SendChatMessageRequest,
        *,
        current_user_id: str | None = None,
    ) -> ChatReplyResponse:
        character = await self._load_character_with_recovery(payload.character_id)
        owner_user_id = getattr(character, "user_id", DEFAULT_OPERATOR_ID)
        # Owner verification: the route already short-circuits, but the
        # service is also called from background pipelines (proactive
        # follow-up dispatcher) that pre-resolved the character. When a
        # caller supplies ``current_user_id`` we enforce it here so the
        # contract holds end-to-end.
        if (
            current_user_id is not None
            and owner_user_id != current_user_id
        ):
            raise ValueError("Character not found")
        if self._subscription_access_guard is not None:
            await self._subscription_access_guard.ensure_character_allowed(character)
        character = await self._maybe_unfreeze_on_interaction(character)
        operator = await self._load_operator(
            user_id=current_user_id or owner_user_id,
        )
        # Bind the ambient cloud actor for this turn so every auxiliary LLM
        # call it fans out to (persona / behaviour extraction, dialogue
        # summarise, ...) resolves cloud identity without threading it. This
        # is the transport-agnostic boundary: it covers the HTTP chat route,
        # the messaging dispatcher, and proactive follow-ups alike. Each turn
        # re-binds at its start, so the no-reset binding is safe even when a
        # background task processes several turns sequentially.
        bind_cloud_actor(character=character)
        presence_frame = payload.resolved_presence_frame()
        content_mode = await self._content_mode_for_character(character)

        conversation = await self._load_conversation(payload.conversation_id, payload.character_id)
        await self._ensure_runtime_message_session_available(
            character=character,
            conversation=conversation,
        )
        # History is merged across every source (web / telegram / line / …)
        # — the character is a single person on every channel, so the
        # prompt should see one unified timeline rather than only this
        # surface's silo. The new turn is still persisted to the surface-
        # bound ``conversation`` below; cross-source merging only affects
        # what the LLM remembers, not where writes land.
        recent_messages = await self._conversation_repository.recent_messages_for_character(
            payload.character_id, limit=_RECENT_MESSAGE_LIMIT,
        )
        prompt_recent_messages, older_dialogue_summary = (
            await self._prepare_prompt_dialogue_context(
                character=character,
                recent_messages=recent_messages,
                content_tolerance=_content_tolerance_for_content_mode(content_mode),
            )
        )
        # Convert any unseen feed likes/comments into memories *before*
        # ranking — otherwise the character can't acknowledge engagement
        # the user just performed before opening chat.
        await self._memorialize_feed_reactions(payload.character_id)
        memories = await self._select_memories(
            payload.character_id,
            query_text=payload.message,
            world_scope=None,
        )
        now_utc = self._resolve_now()
        operator_tz = _operator_timezone(operator)
        active_goals = await self._load_active_goals(payload.character_id)
        (
            current_activity,
            upcoming_activities,
            just_finished_activity,
            completed_today_activities,
            pending_invite_activities,
        ) = await self._load_current_schedule(
            character, now=now_utc, local_tz=operator_tz,
        )
        story_events = await self._ensure_story_events(character, now=now_utc)
        today_local = to_timezone(now_utc, operator_tz).date()
        calendar_context = self._describe_calendar(today_local, operator=operator)
        weather_context = await self._describe_weather(today_local, operator=operator)
        world_event_context = await self._load_world_event_context(
            character, operator,
        )
        upcoming_day_schedules = await self._load_upcoming_day_schedules(
            character.id, today_local=today_local,
        )
        story_arc, upcoming_arc_beats = await self._ensure_story_arc(
            character, today=today_local,
        )
        recent_proactive_messages = await self._load_recent_proactive_messages(
            payload.character_id,
        )
        recent_feed_posts = await self._load_recent_feed_posts(
            payload.character_id,
        )
        self_repetition_hint = self._read_self_repetition_hint(
            conversation.id,
        )

        user_attachments = tuple(
            MessageAttachment(
                kind="image", url=url, mime_type="image/*",
            )
            for url in (payload.attachment_urls or ()) if url
        )
        # Strip the image-trigger marker (e.g. ``/pic``) out of the
        # user's text when the command fired, so downstream context
        # (persisted history, LLM prompt, fallback tool positive) sees
        # only the meaningful content. Leaving ``/pic`` in would mean
        # the next turn's LLM reads ``使用者: 我想看你 /pic`` as
        # dialogue context, which leaks backend mechanics into the
        # roleplay surface.
        forced_fired_early, cleaned_user_message = _resolve_image_trigger(
            character=character, user_message=payload.message,
        )
        user_message = Message(
            role=MessageRole.USER,
            content=cleaned_user_message,
            attachments=user_attachments,
            content_mode=content_mode,
        )
        pending_state = self._state_engine.on_user_message(character.state, cleaned_user_message)
        idle_minutes = _compute_idle_minutes(character.state, now_utc)
        pending_state = await self._maybe_apply_idle_drift(
            character=character,
            pending_state=pending_state,
            idle_minutes=idle_minutes,
            operator=operator,
        )
        # Derived-view overlay (Phase 3.6 lite) — same as the streaming
        # path. Latest turn event's emotion_label overrides the column.
        _send_emotion_events = await self._load_recent_emotion_events(
            character_id=character.id, operator=operator, now=now_utc,
        )
        pending_state = self._overlay_emotion_on_state(
            state=pending_state, events=_send_emotion_events, now=now_utc,
        )

        # Build before any short-circuit mutates the conversation so
        # undo and Layer-4 familiarity can see busy-defer turns too.
        journal = await self._build_pre_turn_journal(
            character=character,
            conversation=conversation,
            turn_started_at=now_utc,
            today_local=today_local,
        )

        # Busy-defer short-circuit: when the character is mid high-busy
        # activity and the decider says "send a brief ack now, real
        # reply later", we persist the ack inline and queue a
        # PendingFollowUp row. Returns to the user without running the
        # post-turn / journal / goal-review pipeline — the eventual
        # full reply (fired by the proactive scheduler tick) is the one
        # that warrants those side effects.
        defer = await self._maybe_defer_reply(
            character=character,
            user_message_text=cleaned_user_message,
            user_attachments=user_attachments,
            current_activity=current_activity,
            pending_state=pending_state,
            conversation=conversation,
            older_dialogue_summary=older_dialogue_summary,
            now_utc=now_utc,
            operator=operator if payload.operator_persona_enabled else None,
            recent_messages=recent_messages,
            recent_proactive_messages=recent_proactive_messages,
            journal=journal,
            content_mode=content_mode,
        )
        if defer is not None:
            _, defer_user_msg, defer_brief_msg, defer_state, _ = defer
            return ChatReplyResponse.build(
                conversation_id=defer[0].id,
                user_message=defer_user_msg,
                assistant_message=defer_brief_msg,
                state=defer_state,
            )

        # Persist the user's turn BEFORE the LLM call so a timeout /
        # network drop can't lose it. The client refreshes → reads the
        # conversation → sees their own message and can retry the reply.
        conversation_with_user = conversation.append(user_message)
        await self._conversation_repository.save(conversation_with_user)

        model, model_id = await self._resolve_main_chat_model(
            character=character,
            payload=payload,
        )
        generation = await self._generate_reply_with_tools(
            character=character,
            conversation=conversation,
            recent_messages=prompt_recent_messages,
            tool_context_messages=recent_messages,
            memories=memories,
            pending_state=pending_state,
            latest_user_message=cleaned_user_message,
            active_goals=active_goals,
            current_activity=current_activity,
            upcoming_activities=upcoming_activities,
            just_finished_activity=just_finished_activity,
            completed_today_activities=completed_today_activities,
            pending_invite_activities=pending_invite_activities,
            story_events=story_events,
            story_arc=story_arc,
            upcoming_arc_beats=upcoming_arc_beats,
            today_local=today_local,
            older_dialogue_summary=older_dialogue_summary,
            recent_proactive_messages=recent_proactive_messages,
            recent_feed_posts=recent_feed_posts,
            self_repetition_hint=self_repetition_hint,
            now=now_utc,
            idle_minutes=idle_minutes,
            model=model,
            model_id=model_id,
            user_attachment_urls=tuple(payload.attachment_urls or ()),
            force_image=forced_fired_early,
            operator=operator,
            operator_persona_enabled=payload.operator_persona_enabled,
            calendar_context=calendar_context,
            weather_context=weather_context,
            world_event_context=world_event_context,
            upcoming_day_schedules=upcoming_day_schedules,
            presence_frame=presence_frame,
            content_tolerance=_content_tolerance_for_model(
                model,
                content_mode=content_mode,
            ),
            routing_content_tolerance=_content_tolerance_for_content_mode(
                content_mode,
            ),
        )
        assistant_text = generation.text
        attachments = generation.attachments
        assistant_message = Message(
            role=MessageRole.ASSISTANT,
            content=assistant_text,
            attachments=tuple(attachments),
            kind=_classify_assistant_kind(assistant_text, attachments),
            content_mode=content_mode,
        )

        now = now_utc
        final_state = self._state_engine.on_assistant_reply(pending_state, assistant_text)
        final_state = final_state.with_active_now(now)
        await self._track(
            character.id, SOURCE_HEURISTIC, character.state, final_state,
            trigger=cleaned_user_message[:80],
        )
        updated_character = character.with_state(final_state)
        updated_conversation = conversation_with_user.append(assistant_message)

        await self._character_repository.save(updated_character)
        await self._conversation_repository.save(updated_conversation)
        self._schedule_nsfw_safe_summary_generation(
            character=character,
            conversation_id=updated_conversation.id,
            user_position=len(updated_conversation.messages) - 2,
            assistant_position=len(updated_conversation.messages) - 1,
            content_mode=content_mode,
            model=model,
            model_id=model_id,
        )
        await self._touch_memories(memories)
        self._maybe_schedule_tts_pregeneration(
            character_id=character.id,
            assistant_text=assistant_text,
            user_id=character.user_id,
            content_mode=content_mode,
        )
        # Post-turn extraction (memory + state + schedule/arc adjustments)
        # runs on every turn — including command-forced ``/pic`` turns.
        # The trigger marker is already stripped from
        # ``cleaned_user_message`` so the post-turn LLM sees clean
        # conversational text; if the user genuinely just typed ``/pic``
        # alone the extractor will return empty memories on its own
        # (post-turn prompt instructs the model to skip when there's
        # nothing worth remembering). Earlier versions skipped this
        # branch under the assumption ``/pic`` was a mechanical command,
        # but with the marker stripped the user message often carries
        # real intent (e.g. ``我想看你在咖啡廳的樣子``) that's worth
        # capturing.
        turn_record_id = str(uuid4())
        post_turn_refs = await self._run_post_turn(
            character=character,
            conversation_id=updated_conversation.id,
            turn_record_id=turn_record_id,
            user_text=cleaned_user_message,
            assistant_text=assistant_text,
            prior_messages=recent_messages,
            persona_enabled=payload.operator_persona_enabled,
            content_mode=content_mode.value,
        )
        self._maybe_schedule_goal_review(
            character=character,
            conversation=updated_conversation,
        )
        self._maybe_schedule_repetition_check(
            character=character,
            conversation=updated_conversation,
        )
        await self._persist_journal(journal)

        await self._record_turn_safely(TurnRecordingDraft(
            id=turn_record_id,
            character_id=character.id,
            kind="chat",
            model_id=(
                generation.trace.model_id
                or model_id
                or str(getattr(model, "provider_id", "") or "")
            ),
            conversation_id=updated_conversation.id,
            prompt_assembled=generation.trace.prompt_assembled,
            prompt_pack_hash=generation.trace.prompt_pack_hash,
            response_text=assistant_text,
            latency_ms=generation.trace.latency_ms,
            prompt_tokens=generation.trace.prompt_tokens,
            completion_tokens=generation.trace.completion_tokens,
            error=generation.trace.error,
            post_turn_refs={
                "source": "send_message",
                "turn_index": len(updated_conversation.messages),
                "memories_used": len(memories),
                "forced_tool": generation.forced_fired,
                "content_mode": content_mode.value,
                "persona_curiosity": persona_curiosity_plan_summary(
                    generation.persona_curiosity_plan,
                    surface="chat",
                ),
                "material_digest": _material_digest_summary(
                    generation.material_digest,
                    enabled=self._prompt_material_digest_enabled,
                ),
                "register_profile": _register_profile_summary(
                    generation.register_profile,
                    enabled=self._register_profile_enabled,
                ),
                "diversity": _diversity_evidence_summary(
                    generation.diversity_evidence,
                ),
                "novelty_gate": _novelty_gate_summary(
                    generation.novelty_verdict,
                    enabled=self._novelty_gate_enabled,
                    retry_count=generation.novelty_retry_count,
                ),
                "presence_frame": presence_frame.to_metadata(),
                **post_turn_refs,
            },
        ))
        await self._record_llm_usage_safely(
            character_id=character.id,
            operator_id=getattr(character, "user_id", DEFAULT_OPERATOR_ID),
            conversation_id=updated_conversation.id,
            turn_record_id=turn_record_id,
            trace=generation.trace,
            provider_id=str(getattr(model, "provider_id", "") or ""),
            source_surface="chat",
            upstream_request_id=str(getattr(model, "last_request_id", "") or ""),
            forced_tool=generation.forced_fired,
        )

        return ChatReplyResponse.build(
            conversation_id=updated_conversation.id,
            user_message=user_message,
            assistant_message=assistant_message,
            state=final_state,
            assistant_turn_record_id=turn_record_id,
        )

    async def send_message_stream(
        self,
        payload: SendChatMessageRequest,
        *,
        current_user_id: str | None = None,
    ) -> tuple[AsyncIterator[str], "StreamFinalizer"]:
        """Start a streaming chat reply.

        Returns a (token_stream, finalizer) tuple.
        The caller should iterate the stream to get tokens, then call
        ``await finalizer.finish(full_text)`` to persist state.
        """
        character = await self._load_character_with_recovery(payload.character_id)
        owner_user_id = getattr(character, "user_id", DEFAULT_OPERATOR_ID)
        if (
            current_user_id is not None
            and owner_user_id != current_user_id
        ):
            raise ValueError("Character not found")
        if self._subscription_access_guard is not None:
            await self._subscription_access_guard.ensure_character_allowed(character)
        character = await self._maybe_unfreeze_on_interaction(character)
        operator = await self._load_operator(
            user_id=current_user_id or owner_user_id,
        )
        # Bind the ambient cloud actor for this turn so every auxiliary LLM
        # call it fans out to (persona / behaviour extraction, dialogue
        # summarise, ...) resolves cloud identity without threading it. This
        # is the transport-agnostic boundary: it covers the HTTP chat route,
        # the messaging dispatcher, and proactive follow-ups alike. Each turn
        # re-binds at its start, so the no-reset binding is safe even when a
        # background task processes several turns sequentially.
        bind_cloud_actor(character=character)
        presence_frame = payload.resolved_presence_frame()
        content_mode = await self._content_mode_for_character(character)

        conversation = await self._load_conversation(payload.conversation_id, payload.character_id)
        await self._ensure_runtime_message_session_available(
            character=character,
            conversation=conversation,
        )
        # History is merged across every source (web / telegram / line / …)
        # — the character is a single person on every channel, so the
        # prompt should see one unified timeline rather than only this
        # surface's silo. The new turn is still persisted to the surface-
        # bound ``conversation`` below; cross-source merging only affects
        # what the LLM remembers, not where writes land.
        recent_messages = await self._conversation_repository.recent_messages_for_character(
            payload.character_id, limit=_RECENT_MESSAGE_LIMIT,
        )
        prompt_recent_messages, older_dialogue_summary = (
            await self._prepare_prompt_dialogue_context(
                character=character,
                recent_messages=recent_messages,
                content_tolerance=_content_tolerance_for_content_mode(content_mode),
            )
        )
        await self._memorialize_feed_reactions(payload.character_id)
        memories = await self._select_memories(
            payload.character_id,
            query_text=payload.message,
            world_scope=None,
        )
        now_utc = self._resolve_now()
        operator_tz = _operator_timezone(operator)
        active_goals = await self._load_active_goals(payload.character_id)
        (
            current_activity,
            upcoming_activities,
            just_finished_activity,
            completed_today_activities,
            pending_invite_activities,
        ) = await self._load_current_schedule(
            character, now=now_utc, local_tz=operator_tz,
        )
        story_events = await self._ensure_story_events(character, now=now_utc)
        today_local = to_timezone(now_utc, operator_tz).date()
        calendar_context = self._describe_calendar(today_local, operator=operator)
        weather_context = await self._describe_weather(today_local, operator=operator)
        world_event_context = await self._load_world_event_context(
            character, operator,
        )
        upcoming_day_schedules = await self._load_upcoming_day_schedules(
            character.id, today_local=today_local,
        )
        story_arc, upcoming_arc_beats = await self._ensure_story_arc(
            character, today=today_local,
        )
        recent_proactive_messages = await self._load_recent_proactive_messages(
            payload.character_id,
        )
        recent_feed_posts = await self._load_recent_feed_posts(
            payload.character_id,
        )
        self_repetition_hint = self._read_self_repetition_hint(
            conversation.id,
        )

        user_attachments = tuple(
            MessageAttachment(
                kind="image", url=url, mime_type="image/*",
            )
            for url in (payload.attachment_urls or ()) if url
        )
        # See the matching block in ``send_message`` — strip any
        # force-trigger marker (``/pic``, etc.) out of the user text
        # so persisted history and prompt context stay clean.
        forced_fired_early, cleaned_user_message = _resolve_image_trigger(
            character=character, user_message=payload.message,
        )
        user_message = Message(
            role=MessageRole.USER,
            content=cleaned_user_message,
            attachments=user_attachments,
            content_mode=content_mode,
        )
        pending_state = self._state_engine.on_user_message(character.state, cleaned_user_message)
        idle_minutes = _compute_idle_minutes(character.state, now_utc)
        pending_state = await self._maybe_apply_idle_drift(
            character=character,
            pending_state=pending_state,
            idle_minutes=idle_minutes,
            operator=operator,
        )

        journal = await self._build_pre_turn_journal(
            character=character,
            conversation=conversation,
            turn_started_at=now_utc,
            today_local=today_local,
        )

        # Busy-defer short-circuit — same flow as the non-streaming
        # path. When the decider chooses to defer, the brief ack is
        # already persisted as the assistant message; we wrap it in a
        # single-chunk stream so the SSE client renders it once and the
        # finalizer just returns the prebuilt response.
        defer = await self._maybe_defer_reply(
            character=character,
            user_message_text=cleaned_user_message,
            user_attachments=user_attachments,
            current_activity=current_activity,
            pending_state=pending_state,
            conversation=conversation,
            older_dialogue_summary=older_dialogue_summary,
            now_utc=now_utc,
            operator=operator if payload.operator_persona_enabled else None,
            recent_messages=recent_messages,
            recent_proactive_messages=recent_proactive_messages,
            journal=journal,
            content_mode=content_mode,
        )
        if defer is not None:
            defer_conv, defer_user_msg, defer_brief_msg, defer_state, _ = defer
            response = ChatReplyResponse.build(
                conversation_id=defer_conv.id,
                user_message=defer_user_msg,
                assistant_message=defer_brief_msg,
                state=defer_state,
            )
            token_stream = _single_chunk_stream(
                defer_brief_msg.content if defer_brief_msg is not None else "",
            )
            finalizer = StreamFinalizer(
                service=self,
                character=character,
                conversation=defer_conv,
                user_message=defer_user_msg,
                pending_state=defer_state,
                used_memories=[],
                prior_messages=recent_messages,
                prebuilt_response=response,
            )
            return token_stream, finalizer

        # Persist the user's turn BEFORE we kick off streaming so a
        # mid-stream network drop / timeout cannot lose it — client
        # refresh will still see the user message in history.
        conversation_with_user = conversation.append(user_message)
        await self._conversation_repository.save(conversation_with_user)

        model, model_id = await self._resolve_main_chat_model(
            character=character,
            payload=payload,
        )
        tool_descriptors = self._describe_tools(character)

        # Tool-use path swaps streaming for a single blocking cycle:
        # we have to see the *full* first reply to decide whether it's
        # a tool call, and the subsequent tool run can take tens of
        # seconds — trying to stream tokens before we know the answer
        # would leak the raw JSON tool-call shape to the user. The
        # streaming adapter is reduced to yielding the final reply
        # text as one chunk, followed by the normal finalizer. Chat UI
        # still shows it as a complete message; the latency hit only
        # applies when tools are enabled on the character.
        if tool_descriptors and self._tool_orchestrator is not None:
            generation = await self._generate_reply_with_tools(
                character=character,
                conversation=conversation,
                recent_messages=prompt_recent_messages,
                tool_context_messages=recent_messages,
                memories=memories,
                pending_state=pending_state,
                latest_user_message=cleaned_user_message,
                active_goals=active_goals,
                current_activity=current_activity,
                upcoming_activities=upcoming_activities,
                just_finished_activity=just_finished_activity,
                completed_today_activities=completed_today_activities,
                pending_invite_activities=pending_invite_activities,
                story_events=story_events,
                story_arc=story_arc,
                upcoming_arc_beats=upcoming_arc_beats,
                today_local=today_local,
                older_dialogue_summary=older_dialogue_summary,
                recent_proactive_messages=recent_proactive_messages,
                recent_feed_posts=recent_feed_posts,
                self_repetition_hint=self_repetition_hint,
                now=now_utc,
                idle_minutes=idle_minutes,
                model=model,
                model_id=model_id,
                user_attachment_urls=tuple(payload.attachment_urls or ()),
                force_image=forced_fired_early,
                operator=operator,
                operator_persona_enabled=payload.operator_persona_enabled,
                calendar_context=calendar_context,
                weather_context=weather_context,
                world_event_context=world_event_context,
                upcoming_day_schedules=upcoming_day_schedules,
                presence_frame=presence_frame,
                content_tolerance=_content_tolerance_for_model(
                    model,
                    content_mode=content_mode,
                ),
                routing_content_tolerance=_content_tolerance_for_content_mode(
                    content_mode,
                ),
                source_surface="chat_stream",
            )
            final_text = generation.text
            attachments = generation.attachments
            token_stream = _single_chunk_stream(final_text)
            finalizer = StreamFinalizer(
                service=self,
                character=character,
                conversation=conversation_with_user,
                user_message=user_message,
                pending_state=pending_state,
                used_memories=memories,
                prior_messages=recent_messages,
                pre_resolved_text=final_text,
                pre_resolved_attachments=attachments,
                journal=journal,
                persona_enabled=payload.operator_persona_enabled,
                trace=generation.trace,
                safe_summary_model=model,
                safe_summary_model_id=model_id,
                forced_tool=generation.forced_fired,
                persona_curiosity_plan=generation.persona_curiosity_plan,
                material_digest=generation.material_digest,
                register_profile=generation.register_profile,
                diversity_evidence=generation.diversity_evidence,
                novelty_verdict=generation.novelty_verdict,
                novelty_retry_count=generation.novelty_retry_count,
                presence_frame=presence_frame,
                content_mode=content_mode,
            )
            return token_stream, finalizer

        # Same vision-inventory computation as the tool-use path — the
        # streaming no-tool branch also needs ``[圖 N]`` markers so
        # history images line up with what we send the model.
        content_tolerance = _content_tolerance_for_model(
            model,
            content_mode=content_mode,
        )
        prompt_messages_for_model = sanitize_messages_for_tolerance(
            prompt_recent_messages,
            content_tolerance=content_tolerance,
        )
        vision_urls, vision_markers = _build_vision_inventory(
            recent_messages=prompt_messages_for_model,
            current_user_urls=tuple(payload.attachment_urls or ()),
        )
        # Recognition routes on CONTENT-driven tolerance (see the tool
        # path): the provider-derived ``content_tolerance`` above is right
        # for history sanitization but wrong for user-uploaded images.
        image_recognition_context = await self._build_image_recognition_context(
            character=character,
            main_model=model,
            attachment_urls=vision_urls,
            content_tolerance=_content_tolerance_for_content_mode(content_mode),
        )
        operator_persona = await self._load_operator_persona(
            character.id, operator,
        )
        operator_persona_lines = self._render_operator_persona_lines(operator_persona)
        peer_roster_lines = await self._load_peer_roster_lines(character.id)
        initial_relationship_lines = await self._load_initial_relationship_lines(
            character.id, operator,
        )
        persona_curiosity_plan = await self._load_persona_curiosity_plan(
            character=character,
            operator=operator,
            enabled=payload.operator_persona_enabled,
            conversation_id=conversation.id,
            recent_dialogue_summary=older_dialogue_summary or "",
            initial_relationship_lines=initial_relationship_lines,
            now=now_utc,
        )
        emotion_events = await self._load_recent_emotion_events(
            character_id=character.id, operator=operator, now=now_utc,
        )
        self_reflections = await self._load_self_reflections(
            character_id=character.id, operator=operator,
        )
        material_digest = await self._load_prompt_material_digest(
            character=character,
            operator=operator,
            emotion_events=emotion_events,
            self_reflections=self_reflections,
            story_events=story_events,
            story_arc=story_arc,
            upcoming_arc_beats=upcoming_arc_beats,
            recent_feed_posts=recent_feed_posts,
            content_tolerance=content_tolerance,
        )
        phrase_habit_lines = await self._load_phrase_habit_lines(character.id)
        register_profile = await self._load_register_profile(
            character=character,
            operator=operator,
            latest_user_message=cleaned_user_message,
            recent_dialogue_summary=older_dialogue_summary or "",
            relationship_context=tuple(
                [
                    *(operator_persona_lines or []),
                    *(initial_relationship_lines or []),
                ],
            ),
            content_tolerance=content_tolerance,
        )
        diversity_evidence = await build_reply_diversity_evidence(
            recent_messages=prompt_messages_for_model,
            self_repetition_hint=self_repetition_hint,
            embedder=self._embedder,
        )
        # Phase 3.6 lite — derived-view overlay: latest turn event's
        # emotion_label may diverge from the column when the rich
        # `emotion_events` path is in play (Item 2), so the prompt sees
        # the most truthful current emotion rather than a stale column.
        # Overlay onto ``pending_state`` (post-idle-drift) so previous
        # mood adjustments aren't lost; numeric columns stay
        # authoritative to avoid double-counting with Phase 3.4 lite.
        pending_state = self._overlay_emotion_on_state(
            state=pending_state, events=emotion_events, now=now_utc,
        )
        owner_user_id = getattr(character, "user_id", DEFAULT_OPERATOR_ID)
        address_preference = await self._fetch_address_preference(
            character_id=character.id,
            operator_id=owner_user_id,
        )
        experiment_overlay = await self._resolve_experiment_overlay(
            character_id=character.id,
            operator_id=owner_user_id,
        )
        resolved_player_address, resolved_character_address = (
            await self._resolve_addresses(
                character, operator, address_preference, operator_persona,
            )
        )
        address_change_lines = await self._load_address_change_lines(
            character, operator,
        )
        async def build_stream_prompt(
            retry_directive: str | None = None,
        ) -> tuple[str, str, tuple[str, ...]]:
            prompt_context = self._prompt_context_builder.build(
                character=character,
                conversation=conversation,
                recent_messages=prompt_messages_for_model,
                memories=memories,
                pending_state=pending_state,
                latest_user_message=cleaned_user_message,
                active_goals=active_goals,
                current_activity=current_activity,
                upcoming_activities=upcoming_activities,
                just_finished_activity=just_finished_activity,
                completed_today_activities=completed_today_activities,
                pending_invite_activities=pending_invite_activities,
                now=now_utc,
                idle_minutes=idle_minutes,
                story_events=story_events,
                story_arc=story_arc,
                upcoming_arc_beats=upcoming_arc_beats,
                today_local=today_local,
                older_dialogue_summary=older_dialogue_summary,
                vision_markers=vision_markers,
                image_recognition_context=image_recognition_context,
                recent_proactive_messages=recent_proactive_messages,
                recent_feed_posts=recent_feed_posts,
                self_repetition_hint=self_repetition_hint,
                phrase_habit_lines=phrase_habit_lines,
                operator=operator,
                operator_persona_lines=operator_persona_lines,
                peer_roster_lines=peer_roster_lines,
                initial_relationship_lines=initial_relationship_lines,
                persona_curiosity_plan=persona_curiosity_plan,
                calendar_context=calendar_context,
                weather_context=weather_context,
                world_event_context=world_event_context,
                upcoming_day_schedules=upcoming_day_schedules,
                emotion_events=emotion_events,
                self_reflections=self_reflections,
                address_preference=address_preference,
                resolved_player_address=resolved_player_address,
                resolved_character_address=resolved_character_address,
                address_change_lines=address_change_lines,
                experiment_overlay=experiment_overlay,
                presence_frame=presence_frame,
                content_tolerance=content_tolerance,
                material_digest=material_digest,
                turn_register_profile=register_profile,
                reply_diversity_evidence=diversity_evidence,
                retry_directive=retry_directive,
            )
            prompt_pack_hash = _last_prompt_pack_hash(self._prompt_context_builder)
            prompt_context, image_urls = await _prepare_vision_prompt(
                model=model,
                prompt=prompt_context,
                attachment_urls=vision_urls,
                public_base_url=self._public_base_url,
                uploads_dir=self._uploads_dir,
                object_storage=self._object_storage,
                image_context=image_recognition_context,
            )
            return prompt_context, prompt_pack_hash, image_urls

        async def generate_buffered_stream_once(
            retry_directive: str | None = None,
        ) -> tuple[str, ChatGenerationTrace]:
            prompt_context, prompt_pack_hash, image_urls = await build_stream_prompt(
                retry_directive,
            )
            try:
                buffered_text, trace = await _generate_capturing(
                    model,
                    prompt_context,
                    image_urls=image_urls,
                    model_id=model_id,
                )
            except Exception as exc:
                failed_trace = _failed_generation_trace(
                    prompt=prompt_context,
                    prompt_pack_hash=prompt_pack_hash,
                    model_id=(
                        model_id
                        or str(getattr(model, "provider_id", "") or "")
                    ),
                    error=exc,
                )
                await self._record_llm_usage_safely(
                    character_id=character.id,
                    operator_id=owner_user_id,
                    conversation_id=conversation_with_user.id,
                    turn_record_id=None,
                    trace=failed_trace,
                    provider_id=str(getattr(model, "provider_id", "") or ""),
                    source_surface="chat_stream",
                    upstream_request_id=str(
                        getattr(model, "last_request_id", "") or "",
                    ),
                )
                raise
            return buffered_text, replace(trace, prompt_pack_hash=prompt_pack_hash)

        should_evaluate_gate = self._reply_quality_gate_required(
            register_profile=register_profile,
            diversity_evidence=diversity_evidence,
        )
        if should_evaluate_gate:
            final_text, trace = await generate_buffered_stream_once()
            novelty_verdict = await self._evaluate_novelty_gate(
                character=character,
                operator=operator,
                response_text=final_text,
                material_digest=material_digest,
                emotion_events=emotion_events,
                self_reflections=self_reflections,
                story_events=story_events,
                story_arc=story_arc,
                upcoming_arc_beats=upcoming_arc_beats,
                recent_feed_posts=recent_feed_posts,
                recent_messages=prompt_messages_for_model,
                self_repetition_hint=self_repetition_hint,
                latest_user_message=cleaned_user_message,
                content_tolerance=content_tolerance,
                register_profile=register_profile,
                diversity_evidence=diversity_evidence,
                persona_context=tuple([
                    f"性格：{', '.join(character.personality)}",
                    f"說話風格：{character.speaking_style}",
                    *initial_relationship_lines,
                ]),
            )
            novelty_retry_count = 0
            if (
                novelty_verdict is not None
                and not novelty_verdict.passes
                and self._novelty_gate_max_retries > 0
            ):
                novelty_retry_count = 1
                final_text, trace = await generate_buffered_stream_once(
                    novelty_verdict.feedback,
                )
            token_stream = _single_chunk_stream(final_text)
            finalizer = StreamFinalizer(
                service=self,
                character=character,
                conversation=conversation_with_user,
                user_message=user_message,
                pending_state=pending_state,
                used_memories=memories,
                prior_messages=recent_messages,
                pre_resolved_text=final_text,
                journal=journal,
                persona_enabled=payload.operator_persona_enabled,
                trace=trace,
                safe_summary_model=model,
                safe_summary_model_id=model_id,
                persona_curiosity_plan=persona_curiosity_plan,
                material_digest=material_digest,
                register_profile=register_profile,
                diversity_evidence=diversity_evidence,
                novelty_verdict=novelty_verdict,
                novelty_retry_count=novelty_retry_count,
                presence_frame=presence_frame,
                content_mode=content_mode,
            )
            return token_stream, finalizer

        prompt_context, prompt_pack_hash, image_urls = await build_stream_prompt()
        # ``sent_prompt`` may differ from ``prompt_context`` when the
        # upstream rejected the image parts and the stream degraded to a
        # no-image retry — the trace must record what was actually sent.
        token_stream, stream_capture, sent_prompt = await _stream_capturing(
            model,
            prompt_context,
            image_urls=image_urls,
            model_id=model_id,
        )

        finalizer = StreamFinalizer(
            service=self,
            character=character,
            conversation=conversation_with_user,
            user_message=user_message,
            pending_state=pending_state,
            used_memories=memories,
            prior_messages=recent_messages,
            journal=journal,
            persona_enabled=payload.operator_persona_enabled,
            trace=ChatGenerationTrace(
                prompt_assembled=sent_prompt,
                prompt_pack_hash=prompt_pack_hash,
                model_id=model_id or str(getattr(model, "provider_id", "") or ""),
            ),
            safe_summary_model=model,
            safe_summary_model_id=model_id,
            stream_capture=stream_capture,
            persona_curiosity_plan=persona_curiosity_plan,
            material_digest=material_digest,
            register_profile=register_profile,
            diversity_evidence=diversity_evidence,
            presence_frame=presence_frame,
            content_mode=content_mode,
        )
        return token_stream, finalizer

    async def _build_pre_turn_journal(
        self,
        *,
        character: Character,
        conversation: Conversation,
        turn_started_at: datetime,
        today_local: date,
    ) -> TurnJournal | None:
        """Capture the pre-turn snapshot needed to undo this turn.

        Called right before the user message is appended. Returning
        ``None`` means the journal subsystem is not wired (no repo) —
        the turn proceeds without recording, and undo will simply
        report "no journal" when asked.

        Failures to read any single subsystem are swallowed: a crashed
        goal-service shouldn't block the chat turn. The missing bits
        stay ``None`` in the journal and the corresponding rollback
        step becomes a no-op.
        """
        if self._journal_repository is None:
            return None
        prev_goals: list[dict] = []
        if self._goal_service is not None:
            try:
                goals = await self._goal_service.list_all_goals(character.id)
                prev_goals = [goal_to_dict(g) for g in goals]
            except Exception:
                _LOGGER.exception("journal: goal snapshot failed")
        prev_arc: dict | None = None
        if self._story_arc_service is not None:
            try:
                arc = await self._story_arc_service.get_active(character.id)
                if arc is not None:
                    prev_arc = arc_to_dict(arc)
            except Exception:
                _LOGGER.exception("journal: arc snapshot failed")
        prev_schedule: dict | None = None
        if self._schedule_service is not None:
            try:
                schedule = await self._schedule_service.get_schedule(
                    character.id, date_=today_local,
                )
                if schedule is not None:
                    prev_schedule = schedule_to_dict(schedule)
            except Exception:
                _LOGGER.exception("journal: schedule snapshot failed")
        return TurnJournal.new(
            conversation_id=conversation.id,
            character_id=character.id,
            turn_index=len(conversation.messages),
            turn_started_at=turn_started_at,
            prev_character_state=state_to_dict(character.state),
            prev_goals=prev_goals,
            prev_active_arc=prev_arc,
            prev_daily_schedule=prev_schedule,
        )

    async def _persist_journal(self, journal: TurnJournal | None) -> None:
        """Persist the finalised journal + GC old entries for the conversation.

        Pruning is best-effort: if the prune query crashes we keep the
        fresh row anyway (worst case the table grows slightly above
        the cap until the next turn). Insertion failure is logged but
        doesn't bubble up — undo is a safety feature, not a hard
        contract.
        """
        if journal is None or self._journal_repository is None:
            return
        try:
            await self._journal_repository.add(journal)
        except Exception:
            _LOGGER.exception("journal: add failed")
            return
        try:
            await self._journal_repository.prune_for_conversation(
                journal.conversation_id, keep=self._journal_keep,
            )
        except Exception:
            _LOGGER.exception("journal: prune failed")

    async def get_latest_conversation(self, character_id: str) -> ConversationResponse | None:
        conversation = await self._conversation_repository.latest_for_character(character_id)
        if conversation is None:
            return None
        return ConversationResponse.from_domain(conversation)

    async def _load_character_with_recovery(self, character_id: str) -> Character:
        """Load a character and apply rest recovery based on idle time."""
        character = await self._character_repository.get(character_id)
        if character is None:
            raise ValueError("Character not found")
        recovered_state = apply_rest_recovery(character.state)
        if recovered_state is not character.state:
            await self._track(character_id, SOURCE_REST_RECOVERY, character.state, recovered_state)
            await self._record_rest_recovery_event(
                character_id=character_id,
                operator_id=getattr(character, "user_id", DEFAULT_OPERATOR_ID),
                before=character.state,
                after=recovered_state,
            )
            character = character.with_state(recovered_state)
        return character

    async def _maybe_unfreeze_on_interaction(self, character: Character) -> Character:
        """處理獨立角色凍結；tenant subscription guard 已在此前執行。

        凍結只停背景活動，但「怎麼解凍」取決於凍結來源：

        - ``idle`` / 舊資料（``frozen_reason is None``）→ 塵封凍結，使用者
          一送訊息即自動解凍恢復背景排程（同時同步 in-memory 實體，讓本
          回合稍後的 ``save`` 不會用 stale 的 ``frozen=True`` 覆寫回去）。
        - legacy ``subscription_lapse`` → 升級期間無法解析 tenant 的相容
          硬鎖，丟 :class:`ChatSubscriptionFrozen`，不自動解凍。
        - ``manual`` → admin 刻意凍結，聊天不自動解凍（黏著），但也不擋
          聊天入口；由 admin 主控台解凍。"""
        if not character.frozen:
            return character
        if character.frozen_reason == FREEZE_REASON_SUBSCRIPTION_LAPSE:
            raise ChatSubscriptionFrozen(
                "character is frozen for a lapsed subscription",
            )
        if character.frozen_reason not in CHAT_THAWABLE_FREEZE_REASONS:
            # e.g. ``manual`` — sticky freeze, admin-cleared only. Chat is
            # still allowed (freeze is background-only), just not thawed.
            return character
        await self._character_repository.set_frozen(
            character.id, frozen=False, now=self._resolve_now(),
        )
        return replace(character, frozen=False, frozen_at=None, frozen_reason=None)

    async def _record_rest_recovery_event(
        self,
        *,
        character_id: str,
        operator_id: str,
        before: CharacterState,
        after: CharacterState,
    ) -> None:
        """Mirror a rest-recovery state change into the event log.

        Low intensity (0.15) + 8h half-life: rest recovery is gradual
        and shouldn't dominate the prompt's 24h summary. ``operator_id``
        is the character owner's user id (= the operator) so multi-user
        deployments keep their emotion streams isolated.
        """
        if self._emotion_event_repository is None:
            return
        fatigue_delta = after.fatigue - before.fatigue
        energy_delta = after.energy - before.energy
        if fatigue_delta == 0 and energy_delta == 0:
            return
        try:
            await self._emotion_event_repository.add(EmotionEvent.new(
                character_id=character_id,
                operator_id=operator_id,
                cause_ref_kind=CAUSE_REST_RECOVERY,
                fatigue_delta=fatigue_delta,
                energy_delta=energy_delta,
                applied_to_state=True,
                intensity=0.15,
                emotion_label="",
                evidence_quote="",
                decay_half_life_minutes=480,
            ))
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "emotion_event_repository.add failed (cause=rest_recovery, character=%s)",
                character_id,
            )

    async def _maybe_apply_idle_drift(
        self,
        *,
        character: Character,
        pending_state: CharacterState,
        idle_minutes: float | None,
        operator: "OperatorProfile | None" = None,
    ) -> CharacterState:
        """Ask the LLM how the absence affected the character, fold the
        result into ``pending_state`` so the upcoming prompt reflects
        the drifted mood. Fail-soft at every layer — if the judge isn't
        wired, the gap is too short, or the LLM call crashes, we return
        ``pending_state`` unchanged and chat proceeds as normal.

        ``operator`` supplies the content language for the drift's
        player-visible ``current_intent`` — without it the judge would
        emit a Chinese intent for a non-Chinese operator."""
        if self._idle_drift_judge is None:
            return pending_state
        if idle_minutes is None or idle_minutes < self._idle_drift_threshold_minutes:
            return pending_state
        operator_language = (
            getattr(operator, "primary_language", "") or ""
        ).strip() or DEFAULT_PRIMARY_LANGUAGE
        try:
            drift = await self._call_idle_drift_judge(
                character=character,
                idle_minutes=idle_minutes,
                operator_primary_language=operator_language,
            )
        except Exception:
            _LOGGER.exception(
                "Idle-drift judge crashed character=%s idle_min=%.1f",
                character.id, idle_minutes,
            )
            return pending_state
        if drift.is_empty:
            return pending_state
        drifted = pending_state.adjust(
            emotion=drift.emotion,
            affection_delta=drift.affection_delta,
            fatigue_delta=drift.fatigue_delta,
            energy_delta=drift.energy_delta,
            current_intent=drift.current_intent,
        )
        try:
            await self._track(
                character.id, SOURCE_LLM_REFINEMENT,
                pending_state, drifted,
                trigger=f"idle_drift {idle_minutes:.0f}min",
            )
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception("Failed to track idle-drift state change")
        await self._record_idle_drift_event(
            character_id=character.id,
            operator_id=getattr(character, "user_id", DEFAULT_OPERATOR_ID),
            before=pending_state,
            after=drifted,
            idle_minutes=idle_minutes,
            label=drift.emotion or "",
        )
        return drifted

    async def _call_idle_drift_judge(
        self,
        *,
        character: Character,
        idle_minutes: float,
        operator_primary_language: str,
    ):
        """Invoke the idle-drift judge, passing the operator language only
        when the wired judge accepts it. Older / stub judges that predate
        the language kwarg keep working (they fall back to their own
        default) instead of raising a TypeError."""
        judge = self._idle_drift_judge
        if _accepts_keyword(judge.judge, "operator_primary_language"):
            return await judge.judge(
                character=character,
                idle_minutes=idle_minutes,
                operator_primary_language=operator_primary_language,
            )
        return await judge.judge(
            character=character, idle_minutes=idle_minutes,
        )

    async def _record_idle_drift_event(
        self,
        *,
        character_id: str,
        operator_id: str,
        before: CharacterState,
        after: CharacterState,
        idle_minutes: float,
        label: str,
    ) -> None:
        """Mirror an idle-drift state change into the event log.

        Intensity scales with idle gap — a half-day of silence is more
        salient than 30 minutes. 6h half-life: drift fades faster than
        a concrete chat-turn emotion (a sulk dissolves once the user
        actually shows up). ``operator_id`` is the character owner so
        per-user emotion streams stay isolated.
        """
        if self._emotion_event_repository is None:
            return
        aff_delta = after.affection - before.affection
        fat_delta = after.fatigue - before.fatigue
        eng_delta = after.energy - before.energy
        if aff_delta == 0 and fat_delta == 0 and eng_delta == 0 and not label.strip():
            return
        # Scale 0.2 ~ 0.6 based on hours alone (saturates at 12h).
        intensity = min(0.6, 0.2 + min(idle_minutes / 60.0, 12.0) * 0.033)
        try:
            await self._emotion_event_repository.add(EmotionEvent.new(
                character_id=character_id,
                operator_id=operator_id,
                cause_ref_kind=CAUSE_IDLE_DRIFT,
                affection_delta=aff_delta,
                fatigue_delta=fat_delta,
                energy_delta=eng_delta,
                applied_to_state=True,
                emotion_label=label.strip(),
                evidence_quote=f"獨處 {idle_minutes:.0f} 分鐘",
                intensity=intensity,
                decay_half_life_minutes=360,
            ))
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "emotion_event_repository.add failed (cause=idle_drift, character=%s)",
                character_id,
            )

    async def _maybe_defer_reply(
        self,
        *,
        character: Character,
        user_message_text: str,
        user_attachments: tuple[MessageAttachment, ...],
        current_activity: "ScheduleActivity | None",
        pending_state: CharacterState,
        conversation: Conversation,
        older_dialogue_summary: str | None,
        now_utc: datetime,
        operator: OperatorProfile | None,
        recent_messages: list[Message],
        recent_proactive_messages: tuple[ProactiveAttempt, ...] = (),
        journal: TurnJournal | None,
        content_mode: MessageContentMode = MessageContentMode.NORMAL,
    ) -> tuple[Conversation, Message, Message | None, CharacterState, PendingFollowUp] | None:
        """Ask the busy-reply decider whether to short-circuit this turn.

        Returns ``None`` when the chat path should proceed normally
        (decider not wired, perf-floor skipped the call, decider said
        ``IMMEDIATE``, or the LLM crashed). Returns a tuple ready for
        the caller to wrap into a ``ChatReplyResponse`` when the
        message was deferred: the conversation has already been
        persisted, character state has been advanced (so idle-detection
        later still works), and the ``PendingFollowUp`` row is queued.
        If an existing busy-defer row is open, this helper records the
        new user message into that row for audit, cancels the row, and
        returns ``None`` so the normal chat path replies immediately.

        The whole helper is fail-soft — any unexpected error returns
        ``None`` so the user always gets a reply via the standard path.
        """
        if (
            self._busy_reply_decider is None
            or self._pending_follow_up_repository is None
        ):
            _LOGGER.debug(
                "busy-defer: skipped (port not wired) character=%s",
                character.id,
            )
            return None
        if await self._cancel_existing_pending_follow_up_for_immediate_reply(
            character=character,
            conversation=conversation,
            user_message_text=user_message_text,
            now_utc=now_utc,
            content_mode=content_mode,
        ):
            return None
        if current_activity is None:
            _LOGGER.info(
                "busy-defer: skipped (no current activity) character=%s",
                character.id,
            )
            return None
        if current_activity.busy_score < _BUSY_DECIDER_INVOKE_FLOOR:
            _LOGGER.info(
                "busy-defer: skipped (busy_score=%.2f below floor=%.2f) "
                "character=%s activity=%s",
                current_activity.busy_score, _BUSY_DECIDER_INVOKE_FLOOR,
                character.id, current_activity.category,
            )
            return None
        _LOGGER.info(
            "busy-defer: invoking decider character=%s activity=%s "
            "busy_score=%.2f user_msg=%r",
            character.id, current_activity.category,
            current_activity.busy_score, user_message_text[:80],
        )
        try:
            relationship_context_lines = (
                await self._load_initial_relationship_lines(
                    character.id, operator,
                )
            )
            interaction_context_lines = tuple(
                await self._load_busy_interaction_lines(
                    character.id,
                    operator,
                    relationship_context_lines=relationship_context_lines,
                ),
            )
            decision = await self._busy_reply_decider.decide(
                character=character,
                user_message=user_message_text,
                current_activity=current_activity,
                recent_dialogue_summary=older_dialogue_summary,
                recent_proactive_attempts=recent_proactive_messages,
                relationship_context_lines=tuple(relationship_context_lines),
                interaction_context_lines=interaction_context_lines,
                now=now_utc,
                local_tz=_operator_timezone(operator),
                # The brief ack is sent straight to the player, so it must
                # follow the operator's content language (bug B2 class).
                operator_primary_language=(
                    getattr(operator, "primary_language", "") or ""
                ).strip() or DEFAULT_PRIMARY_LANGUAGE,
            )
        except Exception:
            _LOGGER.exception(
                "busy_reply_decider crashed character=%s", character.id,
            )
            return None
        _LOGGER.info(
            "busy-defer: decider returned mode=%s brief=%r defer_until=%s "
            "reason=%r character=%s",
            decision.mode.value, decision.brief_reply[:80],
            decision.defer_until.isoformat() if decision.defer_until else None,
            decision.defer_reason, character.id,
        )
        if not decision.is_defer:
            return None
        brief = decision.brief_reply.strip()
        if not brief:
            _LOGGER.info(
                "busy-defer: decider said defer but brief was empty — "
                "falling back to immediate character=%s",
                character.id,
            )
            return None

        user_msg = Message(
            role=MessageRole.USER,
            content=user_message_text,
            attachments=user_attachments,
            content_mode=content_mode,
        )
        brief_msg = Message(
            role=MessageRole.ASSISTANT,
            content=brief,
            kind=_classify_assistant_kind(brief, ()),
            content_mode=content_mode,
        )
        conv_with_user = conversation.append(user_msg)
        conv_with_brief = conv_with_user.append(brief_msg)
        try:
            await self._conversation_repository.save(conv_with_brief)
        except Exception:
            _LOGGER.exception(
                "failed to persist defer turn conversation=%s",
                conversation.id,
            )
            return None

        final_state = self._state_engine.on_assistant_reply(
            pending_state, brief,
        )
        final_state = final_state.with_active_now(now_utc)
        try:
            await self._track(
                character.id, SOURCE_HEURISTIC,
                character.state, final_state,
                trigger=f"busy_defer:{user_message_text[:60]}",
            )
        except Exception:  # pragma: no cover - defensive
            _LOGGER.exception("failed to track defer state change")
        try:
            await self._character_repository.save(
                character.with_state(final_state),
            )
        except Exception:
            _LOGGER.exception(
                "failed to persist character state on defer character=%s",
                character.id,
            )

        try:
            follow_up = await self._upsert_pending_follow_up(
                character_id=character.id,
                conversation_id=conv_with_brief.id,
                user_message_text=user_message_text,
                decision=decision,
                current_activity=current_activity,
                now=now_utc,
                content_mode=content_mode,
            )
        except Exception:
            _LOGGER.exception(
                "failed to upsert pending follow-up character=%s",
                character.id,
            )
            # We've already persisted the brief ack to the user — falling
            # back to "normal reply" here would mean the user sees both
            # the brief ack AND a full reply, which is worse than just
            # losing the follow-up row. Surface the brief and exit; the
            # user can prompt again later if needed.
            follow_up = PendingFollowUp.new(
                character_id=character.id,
                conversation_id=conv_with_brief.id,
                first_message=PendingFollowUpMessage.new(
                    content=user_message_text,
                    queued_at=now_utc,
                    content_mode=content_mode,
                ),
                brief_reply=brief,
                defer_reason=decision.defer_reason,
                scheduled_for=(
                    decision.defer_until
                    or current_activity.end_at
                ),
                activity_id=current_activity.id,
                now=now_utc,
            )

        _LOGGER.info(
            "busy-defer: queued follow-up id=%s character=%s "
            "scheduled_for=%s queued_msg_count=%d",
            follow_up.id, character.id,
            follow_up.scheduled_for.isoformat(),
            len(follow_up.messages),
        )
        await self._run_persona_extraction(
            character=character,
            operator=operator,
            conversation_id=conv_with_brief.id,
            user_text=user_message_text,
            assistant_text=brief,
            prior_messages=recent_messages,
            content_mode=content_mode,
        )
        await self._persist_journal(journal)
        return conv_with_brief, user_msg, brief_msg, final_state, follow_up

    async def _cancel_existing_pending_follow_up_for_immediate_reply(
        self,
        *,
        character: Character,
        conversation: Conversation,
        user_message_text: str,
        now_utc: datetime,
        content_mode: MessageContentMode = MessageContentMode.NORMAL,
    ) -> bool:
        """Cancel an open busy-defer row so this turn gets a normal reply.

        The new user message is appended before cancellation for audit
        and debugging. We then let the normal chat path persist the user
        turn and assistant reply, so the user never gets a silent void
        while an earlier busy-defer promise is still open.
        """
        existing = await self._find_open_busy_defer_for_conversation(
            character_id=character.id,
            conversation_id=conversation.id,
        )
        if existing is None:
            return False

        merged = existing.appended(
            PendingFollowUpMessage.new(
                content=user_message_text,
                queued_at=now_utc,
                content_mode=content_mode,
            ),
            now=now_utc,
        ).cancelled(now=now_utc)
        try:
            assert self._pending_follow_up_repository is not None
            await self._pending_follow_up_repository.save(merged)
        except Exception:
            _LOGGER.exception(
                "busy-defer: failed to cancel existing pending follow-up "
                "character=%s conversation=%s",
                character.id, conversation.id,
            )
            return True
        _LOGGER.info(
            "busy-defer: cancelled existing follow-up id=%s character=%s "
            "queued_msg_count=%d; this turn will reply immediately",
            merged.id, character.id, len(merged.messages),
        )
        return True

    async def _upsert_pending_follow_up(
        self,
        *,
        character_id: str,
        conversation_id: str,
        user_message_text: str,
        decision: BusyDecision,
        current_activity: "ScheduleActivity",
        now: datetime,
        content_mode: MessageContentMode = MessageContentMode.NORMAL,
    ) -> PendingFollowUp:
        """Merge-or-create the queued follow-up row.

        A normal call site cancels any existing busy-defer row before
        reaching this method so the user gets an immediate full reply on
        the next message. The merge branch below remains as a
        race-condition fallback: never drop user text if another request
        created a row between the safety-net check and this upsert.

        For a brand-new defer we honour the decider's ``defer_until``
        if present, falling back to the current activity's end.
        """
        assert self._pending_follow_up_repository is not None
        repo = self._pending_follow_up_repository
        existing = await self._find_open_busy_defer_for_conversation(
            character_id=character_id,
            conversation_id=conversation_id,
        )
        new_message = PendingFollowUpMessage.new(
            content=user_message_text,
            queued_at=now,
            content_mode=content_mode,
        )
        if existing is not None:
            merged = existing.appended(new_message, now=now)
            await repo.save(merged)
            return merged
        scheduled_for = (
            decision.defer_until
            if decision.defer_until is not None
            else current_activity.end_at
        )
        follow_up = PendingFollowUp.new(
            character_id=character_id,
            conversation_id=conversation_id,
            first_message=new_message,
            brief_reply=decision.brief_reply,
            defer_reason=decision.defer_reason,
            scheduled_for=scheduled_for,
            activity_id=current_activity.id,
            now=now,
        )
        await repo.add(follow_up)
        return follow_up

    async def _find_open_busy_defer_for_conversation(
        self,
        *,
        character_id: str,
        conversation_id: str,
    ) -> PendingFollowUp | None:
        """Return the newest open busy-defer row for this conversation."""
        if self._pending_follow_up_repository is None:
            return None
        rows = await self._pending_follow_up_repository.list_open_for_character(
            character_id,
        )
        candidates = [
            row for row in rows
            if row.conversation_id == conversation_id
            and row.kind == PendingFollowUpKind.BUSY_DEFER
        ]
        if not candidates:
            return None
        candidates.sort(key=lambda row: row.queued_at, reverse=True)
        return candidates[0]

    async def _load_operator(
        self,
        *,
        user_id: str | None = None,
    ) -> "OperatorProfile | None":
        """Resolve the operator profile of the current chat turn.

        Multi-user note: in pre-auth single-operator deployments this
        returned ``operator_profile_service.get_current()`` — which
        always reads the default singleton row. After multi-user auth
        the operator is whichever user owns the character being
        chatted with, so callers thread ``user_id`` through and we look
        it up directly. ``user_id=None`` keeps the legacy path for
        background callers (post-turn, undo, persona dream) that
        haven't been ported yet.

        Returns ``None`` when no operator profile service is wired or
        the lookup fails — the prompt builder degrades to legacy
        "使用者" wording in that case."""
        if self._operator_profile_service is None:
            return None
        try:
            if user_id is not None:
                getter = getattr(
                    self._operator_profile_service, "get_for_user", None,
                )
                if getter is not None:
                    return await getter(user_id)
            return await self._operator_profile_service.get_current()
        except Exception:  # pragma: no cover - defensive
            return None

    async def _load_conversation(self, conversation_id: str | None, character_id: str) -> Conversation:
        if conversation_id is None:
            conversation = Conversation.start(character_id=character_id)
            await self._conversation_repository.save(conversation)
            return conversation

        conversation = await self._conversation_repository.get(conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found")
        return conversation

    async def _select_memories(
        self,
        character_id: str,
        *,
        query_text: str | None = None,
        world_scope: "WorldScope" = "all",
    ) -> list[MemoryItem]:
        """Pick memory items for the prompt.

        When the embedder is configured and a ``query_text`` is given,
        we run a semantic search for the top-``_MEMORY_POOL_SIZE``
        candidates and blend (salience + recency + similarity) through
        the hybrid ranker. Otherwise we fall back to the legacy
        recency-ordered pool + 2-factor rank.

        ``world_scope`` is retained for legacy memory rows that may have
        been tagged by the removed world system. Chat passes ``None`` so
        standalone conversations only load non-world-scoped memories.
        Defaults to ``"all"`` so callers that do not care about that
        legacy isolation, such as consolidation passes, keep working.
        """
        query_embedding = await self._embed_query(query_text)
        if query_embedding is not None:
            try:
                candidates = await self._memory_repository.query_semantic(
                    character_id,
                    query_embedding,
                    limit=_MEMORY_POOL_SIZE,
                    world_scope=world_scope,
                )
            except Exception:
                _LOGGER.exception("Semantic memory query failed; falling back to recency pool")
                candidates = None
            if candidates:
                return rank_hybrid(candidates, top_k=_MEMORY_PROMPT_TOP_K)

        pool = await self._memory_repository.query(
            character_id,
            limit=_MEMORY_POOL_SIZE,
            world_scope=world_scope,
        )
        return rank(pool, top_k=_MEMORY_PROMPT_TOP_K)

    def _describe_tools(self, character: Character) -> list[PromptToolDescriptor]:
        """Resolve the tools available to this character for prompt display.

        Returns an empty list when either the registry is not wired or
        the character's ``allowed_tools`` filters everything out. The
        chat loop uses this to decide whether to enter the tool cycle
        at all — an empty list means fall straight through to the
        streaming path unchanged.
        """
        if self._tool_registry is None:
            return []
        tools = self._tool_registry.list_for_character(character)
        return [
            PromptToolDescriptor(
                name=tool.name,
                description=tool.description,
                parameters_schema=tool.parameters_schema,
            )
            for tool in tools
        ]

    async def _generate_reply_with_tools(
        self,
        *,
        character: Character,
        conversation: Conversation,
        recent_messages: list[Message],
        tool_context_messages: list[Message] | None = None,
        memories: list[MemoryItem],
        pending_state: CharacterState,
        latest_user_message: str,
        active_goals: list[CharacterGoal],
        current_activity: ScheduleActivity | None,
        upcoming_activities: list[ScheduleActivity],
        just_finished_activity: ScheduleActivity | None = None,
        completed_today_activities: list[ScheduleActivity] | None = None,
        pending_invite_activities: list[ScheduleActivity] | None = None,
        story_events: list[StoryEvent] | None = None,
        story_arc: "StoryArc | None" = None,
        upcoming_arc_beats: "list[StoryArcBeat] | None" = None,
        today_local: "date | None" = None,
        older_dialogue_summary: str | None = None,
        recent_proactive_messages: tuple[ProactiveAttempt, ...] = (),
        recent_feed_posts: tuple[FeedPost, ...] = (),
        self_repetition_hint: str | None = None,
        now: datetime,
        idle_minutes: float | None,
        model,
        model_id: str | None = None,
        user_attachment_urls: tuple[str, ...] = (),
        force_image: bool | None = None,
        operator: "OperatorProfile | None" = None,
        operator_persona_enabled: bool = True,
        calendar_context: str = "",
        weather_context: str = "",
        world_event_context: tuple[str, ...] = (),
        upcoming_day_schedules: list | None = None,
        presence_frame: PresenceFrame | None = None,
        content_tolerance: str = CONTENT_TOLERANCE_FRONTIER,
        routing_content_tolerance: str = CONTENT_TOLERANCE_FRONTIER,
        source_surface: str = "chat",
    ) -> ChatGenerationResult:
        """Multi-hop tool-use cycle.

        Returns ``ChatGenerationResult``. ``forced_fired`` is true when
        the fixed ``/pic`` image trigger fired on this turn — kept on
        the return value for telemetry / future routing decisions, but
        **no longer gates anything**: post-turn memory / state /
        schedule / arc extraction now runs on every turn including
        forced ones. The
        trigger marker is stripped from the persisted user message
        upstream, so the post-turn LLM sees a clean conversational
        line and decides on its own whether the turn carried anything
        worth remembering.

        The trigger just mandates *that* a tool must be called this
        turn; the LLM still drives argument selection from context.

        Hop 0: offer tools + let the model either respond directly or
        emit a JSON tool call. If it's a tool call, run the tool.
        Hop 1: suppress the tools block and re-prompt with the tool
        result so the model must produce a user-facing reply.

        No tools configured → single model call, same shape as before
        the tool-use feature landed.
        """
        recent_messages = sanitize_messages_for_tolerance(
            recent_messages,
            content_tolerance=content_tolerance,
        )
        if tool_context_messages is not None:
            tool_context_messages = sanitize_messages_for_tolerance(
                tool_context_messages,
                content_tolerance=content_tolerance,
            )
        tool_descriptors = self._describe_tools(character)
        # Vision inventory — pick which images we'll forward this turn
        # (cap = ``_VISION_HISTORY_LIMIT``, FIFO eviction). Computed
        # once and reused by both prompt builder (for ``[圖 N]``
        # markers) and ``_prepare_vision_prompt`` (to feed the model).
        vision_urls, vision_markers = _build_vision_inventory(
            recent_messages=recent_messages,
            current_user_urls=user_attachment_urls,
        )
        # Image recognition routes on CONTENT-driven tolerance, not the
        # main-model-provider-derived ``content_tolerance``: its input is
        # user-uploaded images, so a text-only / non-frontier main model
        # must not drag recognition onto the community NSFW target.
        image_recognition_context = await self._build_image_recognition_context(
            character=character,
            main_model=model,
            attachment_urls=vision_urls,
            content_tolerance=routing_content_tolerance,
        )
        # Load operator-persona prompt lines once outside the hop loop —
        # the lines are stable for the duration of one turn and the
        # service hits the DB to assemble them.
        operator_persona = (
            await self._load_operator_persona(character.id, operator)
            if operator_persona_enabled else None
        )
        operator_persona_lines = self._render_operator_persona_lines(operator_persona)
        peer_roster_lines = await self._load_peer_roster_lines(character.id)
        initial_relationship_lines = await self._load_initial_relationship_lines(
            character.id, operator,
        )
        persona_curiosity_plan = await self._load_persona_curiosity_plan(
            character=character,
            operator=operator,
            enabled=operator_persona_enabled,
            conversation_id=conversation.id,
            recent_dialogue_summary=older_dialogue_summary or "",
            initial_relationship_lines=initial_relationship_lines,
            now=now,
        )
        emotion_events = await self._load_recent_emotion_events(
            character_id=character.id, operator=operator, now=now,
        )
        self_reflections = await self._load_self_reflections(
            character_id=character.id, operator=operator,
        )
        material_digest = await self._load_prompt_material_digest(
            character=character,
            operator=operator,
            emotion_events=emotion_events,
            self_reflections=self_reflections,
            story_events=story_events,
            story_arc=story_arc,
            upcoming_arc_beats=upcoming_arc_beats,
            recent_feed_posts=recent_feed_posts,
            content_tolerance=content_tolerance,
        )
        phrase_habit_lines = await self._load_phrase_habit_lines(character.id)
        register_profile = await self._load_register_profile(
            character=character,
            operator=operator,
            latest_user_message=latest_user_message,
            recent_dialogue_summary=older_dialogue_summary or "",
            relationship_context=tuple(
                [
                    *(operator_persona_lines or []),
                    *(initial_relationship_lines or []),
                ],
            ),
            content_tolerance=content_tolerance,
        )
        diversity_evidence = await build_reply_diversity_evidence(
            recent_messages=recent_messages,
            self_repetition_hint=self_repetition_hint,
            embedder=self._embedder,
        )
        # ``getattr`` fallback handles legacy unit tests that pass the
        # ``CharacterResponse`` DTO straight in — production callers
        # always hand over a domain ``Character`` with ``user_id`` set.
        owner_user_id = getattr(character, "user_id", DEFAULT_OPERATOR_ID)
        # Operator's content language for any deterministic (non-LLM)
        # player-visible strings we emit below — e.g. the truncation
        # apology bubble when a tool-call JSON comes back unparseable.
        # An en/ja operator must not receive a zh-TW apology.
        operator_primary_language = getattr(operator, "primary_language", "") or ""
        address_preference = await self._fetch_address_preference(
            character_id=character.id,
            operator_id=owner_user_id,
        )
        experiment_overlay = await self._resolve_experiment_overlay(
            character_id=character.id,
            operator_id=owner_user_id,
        )
        resolved_player_address, resolved_character_address = (
            await self._resolve_addresses(
                character, operator, address_preference, operator_persona,
            )
        )
        address_change_lines = await self._load_address_change_lines(
            character, operator,
        )
        tool_messages = tool_context_messages or recent_messages
        if not tool_descriptors or self._tool_orchestrator is None:
            async def generate_no_tool_once(
                retry_directive: str | None = None,
            ) -> tuple[str, ChatGenerationTrace]:
                prompt = self._prompt_context_builder.build(
                    character=character,
                    conversation=conversation,
                    recent_messages=recent_messages,
                    memories=memories,
                    pending_state=pending_state,
                    latest_user_message=latest_user_message,
                    active_goals=active_goals,
                    current_activity=current_activity,
                    upcoming_activities=upcoming_activities,
                    just_finished_activity=just_finished_activity,
                    completed_today_activities=completed_today_activities,
                    pending_invite_activities=pending_invite_activities,
                    now=now,
                    idle_minutes=idle_minutes,
                    story_events=story_events,
                    story_arc=story_arc,
                    upcoming_arc_beats=upcoming_arc_beats,
                    today_local=today_local,
                    older_dialogue_summary=older_dialogue_summary,
                    vision_markers=vision_markers,
                    image_recognition_context=image_recognition_context,
                    recent_proactive_messages=recent_proactive_messages,
                    recent_feed_posts=recent_feed_posts,
                    self_repetition_hint=self_repetition_hint,
                    phrase_habit_lines=phrase_habit_lines,
                    operator=operator,
                    operator_persona_lines=operator_persona_lines,
                    peer_roster_lines=peer_roster_lines,
                    initial_relationship_lines=initial_relationship_lines,
                    persona_curiosity_plan=persona_curiosity_plan,
                    calendar_context=calendar_context,
                    weather_context=weather_context,
                    world_event_context=world_event_context,
                    upcoming_day_schedules=upcoming_day_schedules,
                    emotion_events=emotion_events,
                    self_reflections=self_reflections,
                    address_preference=address_preference,
                    resolved_player_address=resolved_player_address,
                    resolved_character_address=resolved_character_address,
                    address_change_lines=address_change_lines,
                    experiment_overlay=experiment_overlay,
                    presence_frame=presence_frame,
                    content_tolerance=content_tolerance,
                    material_digest=material_digest,
                    turn_register_profile=register_profile,
                    reply_diversity_evidence=diversity_evidence,
                    retry_directive=retry_directive,
                )
                prompt_pack_hash = _last_prompt_pack_hash(self._prompt_context_builder)
                prompt, image_urls = await _prepare_vision_prompt(
                    model=model,
                    prompt=prompt,
                    attachment_urls=vision_urls,
                    public_base_url=self._public_base_url,
                    uploads_dir=self._uploads_dir,
                    object_storage=self._object_storage,
                    image_context=image_recognition_context,
                )
                try:
                    generated_text, trace = await _generate_capturing(
                        model,
                        prompt,
                        image_urls=image_urls,
                        model_id=model_id,
                    )
                except Exception as exc:
                    failed_trace = _failed_generation_trace(
                        prompt=prompt,
                        prompt_pack_hash=prompt_pack_hash,
                        model_id=(
                            model_id
                            or str(getattr(model, "provider_id", "") or "")
                        ),
                        error=exc,
                    )
                    await self._record_llm_usage_safely(
                        character_id=character.id,
                        operator_id=owner_user_id,
                        conversation_id=conversation.id,
                        turn_record_id=None,
                        trace=failed_trace,
                        provider_id=str(getattr(model, "provider_id", "") or ""),
                        source_surface=source_surface,
                        upstream_request_id=str(
                            getattr(model, "last_request_id", "") or "",
                        ),
                    )
                    raise
                return generated_text, replace(trace, prompt_pack_hash=prompt_pack_hash)

            text, trace = await generate_no_tool_once()
            novelty_verdict = None
            if self._reply_quality_gate_required(
                register_profile=register_profile,
                diversity_evidence=diversity_evidence,
            ):
                novelty_verdict = await self._evaluate_novelty_gate(
                    character=character,
                    operator=operator,
                    response_text=text,
                    material_digest=material_digest,
                    emotion_events=emotion_events,
                    self_reflections=self_reflections,
                    story_events=story_events,
                    story_arc=story_arc,
                    upcoming_arc_beats=upcoming_arc_beats,
                    recent_feed_posts=recent_feed_posts,
                    recent_messages=recent_messages,
                    self_repetition_hint=self_repetition_hint,
                    latest_user_message=latest_user_message,
                    content_tolerance=content_tolerance,
                    register_profile=register_profile,
                    diversity_evidence=diversity_evidence,
                    persona_context=tuple([
                        f"性格：{', '.join(character.personality)}",
                        f"說話風格：{character.speaking_style}",
                        *initial_relationship_lines,
                    ]),
                )
            novelty_retry_count = 0
            if (
                novelty_verdict is not None
                and not novelty_verdict.passes
                and self._novelty_gate_max_retries > 0
            ):
                novelty_retry_count = 1
                text, trace = await generate_no_tool_once(novelty_verdict.feedback)
            return ChatGenerationResult(
                text=text,
                attachments=[],
                forced_fired=False,
                trace=trace,
                persona_curiosity_plan=persona_curiosity_plan,
                material_digest=material_digest,
                register_profile=register_profile,
                diversity_evidence=diversity_evidence,
                novelty_verdict=novelty_verdict,
                novelty_retry_count=novelty_retry_count,
            )

        tool_outcomes: list[ToolOutcomeMessage] = []
        collected: list[MessageAttachment] = []
        last_text = ""
        traces: list[ChatGenerationTrace] = []
        # Cap how many times we'll retry when the model emits an
        # un-parseable tool call. Past runs showed models in a slow /
        # truncating state could eat the full ``_MAX_TOOL_HOPS`` budget
        # emitting the same malformed JSON four times in a row — each
        # attempt is a full ``model.generate`` round-trip, so it looked
        # (and cost) like an infinite loop from the user's side. One
        # retry is enough signal; beyond that we bail with a polite
        # placeholder rather than hammer the backend.
        parse_retries_left = 1
        # Track each ``(tool, args)`` we've already run so a stuck model
        # can't burn the hop budget by re-emitting the same call in a
        # loop (another "feels infinite" failure mode). Hashed to JSON
        # so equivalent arg dicts collapse to the same key.
        seen_calls: set[tuple[str, str]] = set()
        image_tool_executed = False

        # Give visual tools a window on the last few turns so they can
        # resolve scene references like "那樣的感覺" / "剛剛講的那家店"
        # that the raw ``positive`` string alone doesn't carry. Built
        # once and reused for every orchestrator call this turn.
        recent_dialogue = _format_recent_dialogue(
            tool_messages, latest_user_message=latest_user_message,
        )

        # Resolve the user's current-turn attachments to LLM-fetchable
        # form (storage-backed data: URL for our media, absolute
        # http(s):// otherwise) so visual tools' own vision-capable
        # rewriter can read them. We do the resolution here (not
        # inside the tool) because chat service owns storage/public URL
        # translation. Empty when the user uploaded nothing.
        resolved_user_attachment_urls = tuple(
            converted
            for converted in [
                await _to_vision_url_with_storage(
                    u,
                    uploads_dir=self._uploads_dir,
                    public_base_url=self._public_base_url,
                    object_storage=self._object_storage,
                )
                for u in (user_attachment_urls or ())
                if u
            ]
            if converted is not None
        )

        # Pattern-triggered forced tool use. A regex match on the
        # user message mandates that this turn *must* route through
        # ``generate_image`` — but the LLM still picks arguments from
        # conversation context, so the image reflects the scene being
        # discussed rather than the literal ``/pic`` command text.
        # The flag only gates behaviour on the first tool-offering hop:
        # once the model has emitted (and we have executed) the tool
        # call, subsequent hops return to the normal framing.
        # Caller (``send_message`` / ``send_message_stream``) has
        # usually already computed this against the *raw* user input
        # and stripped the trigger marker from ``latest_user_message``
        # before handing it to us — re-detecting here on the cleaned
        # text would miss the match. Accept an explicit override and
        # only fall back to self-detection for legacy call sites.
        if force_image is None:
            forced_fired = _should_force_image_tool(
                character=character, user_message=latest_user_message,
            )
        else:
            forced_fired = force_image
        forced_pending = forced_fired
        for hop in range(_MAX_TOOL_HOPS):
            # Offer tools on every hop except the last — the final
            # hop always hides the tools block to force a user-facing
            # reply even if the model would otherwise keep chaining.
            # Previous tool outcomes stay visible via tool_outcomes so
            # the model can read what it already learned.
            is_last_hop = hop == _MAX_TOOL_HOPS - 1
            tools_for_hop = tool_descriptors if not is_last_hop else []
            # Forced directive only applies while the forced tool hasn't
            # been executed yet. After a successful (or failed) forced
            # call, clear the flag so hop 1+ lets the model write a
            # normal wrap-up reply instead of being told to emit JSON
            # again.
            forced_directive = (
                _FORCED_IMAGE_TOOL_NAME if forced_pending and tools_for_hop else None
            )
            prompt = self._prompt_context_builder.build(
                character=character,
                conversation=conversation,
                recent_messages=recent_messages,
                memories=memories,
                pending_state=pending_state,
                latest_user_message=latest_user_message,
                active_goals=active_goals,
                current_activity=current_activity,
                upcoming_activities=upcoming_activities,
                just_finished_activity=just_finished_activity,
                completed_today_activities=completed_today_activities,
                pending_invite_activities=pending_invite_activities,
                now=now,
                idle_minutes=idle_minutes,
                available_tools=tools_for_hop,
                tool_outcomes=tool_outcomes,
                forced_tool_name=forced_directive,
                story_events=story_events,
                story_arc=story_arc,
                upcoming_arc_beats=upcoming_arc_beats,
                today_local=today_local,
                older_dialogue_summary=older_dialogue_summary,
                vision_markers=vision_markers,
                image_recognition_context=image_recognition_context,
                recent_proactive_messages=recent_proactive_messages,
                recent_feed_posts=recent_feed_posts,
                self_repetition_hint=self_repetition_hint,
                phrase_habit_lines=phrase_habit_lines,
                operator=operator,
                operator_persona_lines=operator_persona_lines,
                peer_roster_lines=peer_roster_lines,
                initial_relationship_lines=initial_relationship_lines,
                persona_curiosity_plan=persona_curiosity_plan,
                calendar_context=calendar_context,
                weather_context=weather_context,
                world_event_context=world_event_context,
                upcoming_day_schedules=upcoming_day_schedules,
                emotion_events=emotion_events,
                self_reflections=self_reflections,
                address_preference=address_preference,
                resolved_player_address=resolved_player_address,
                resolved_character_address=resolved_character_address,
                address_change_lines=address_change_lines,
                experiment_overlay=experiment_overlay,
                presence_frame=presence_frame,
                content_tolerance=content_tolerance,
                material_digest=material_digest,
                turn_register_profile=register_profile,
                reply_diversity_evidence=diversity_evidence,
            )
            prompt_pack_hash = _last_prompt_pack_hash(self._prompt_context_builder)
            prompt_for_model, image_urls = await _prepare_vision_prompt(
                model=model,
                prompt=prompt,
                attachment_urls=vision_urls,
                public_base_url=self._public_base_url,
                uploads_dir=self._uploads_dir,
                object_storage=self._object_storage,
                image_context=image_recognition_context,
            )
            try:
                text, trace = await _generate_capturing(
                    model,
                    prompt_for_model,
                    image_urls=image_urls,
                    model_id=model_id,
                )
            except Exception as exc:
                failed_trace = _failed_generation_trace(
                    prompt=prompt_for_model,
                    prompt_pack_hash=prompt_pack_hash,
                    model_id=(
                        model_id
                        or str(getattr(model, "provider_id", "") or "")
                    ),
                    error=exc,
                )
                await self._record_llm_usage_safely(
                    character_id=character.id,
                    operator_id=owner_user_id,
                    conversation_id=conversation.id,
                    turn_record_id=None,
                    trace=failed_trace,
                    provider_id=str(getattr(model, "provider_id", "") or ""),
                    source_surface=source_surface,
                    upstream_request_id=str(
                        getattr(model, "last_request_id", "") or "",
                    ),
                )
                raise
            trace = replace(trace, prompt_pack_hash=prompt_pack_hash)
            traces.append(trace)
            last_text = text
            if not tools_for_hop:
                break
            call = parse_tool_call(text)
            if call is None and forced_pending:
                # Forced trigger but LLM ignored the directive. Fall
                # back to a synthesised call so the operator's `/pic`
                # still produces an image. ``positive`` gets the raw
                # user message; the image tool's own prompt rewriter
                # will clean it up, and passing the whole line (vs
                # stripping command prefixes) lets the rewriter see
                # any scene hint the user tacked on.
                call = ToolCall(
                    name=_FORCED_IMAGE_TOOL_NAME,
                    arguments={"positive": (latest_user_message or "").strip()},
                )
                _LOGGER.info(
                    "chat tool-use: forced image trigger — LLM skipped, "
                    "synthesising fallback call",
                )
            if call is None:
                # The model tried to emit a tool call but the JSON came
                # out malformed (usually truncated by max_tokens, mid-
                # stream drop, or the model giving up mid-argument).
                # Suppress the raw ``{"tool": ...`` blob — leaking it
                # into the chat bubble looks like a bug to the user —
                # and feed the failure back as a tool outcome so the
                # model gets ONE chance to retry with a cleaner call.
                # Retries are capped (see ``parse_retries_left``) so a
                # persistently-truncating backend can't eat the whole
                # hop budget emitting the same broken JSON four times.
                if looks_like_tool_call_attempt(text) and parse_retries_left > 0:
                    parse_retries_left -= 1
                    _LOGGER.warning(
                        "chat tool-use: malformed tool call — hop=%d, "
                        "retries_left=%d, text head=%r",
                        hop, parse_retries_left, text[:200],
                    )
                    tool_outcomes.append(
                        ToolOutcomeMessage(
                            tool_name="(parse)", ok=False, output_text="",
                            error=(
                                "你上一輪的 JSON 不完整（可能被截斷）。"
                                "本輪請重新輸出一段完整、可被解析的 JSON 工具呼叫，"
                                "或若不再需要工具就直接用角色台詞回覆。"
                            ),
                        ),
                    )
                    last_text = localized_fallback_text(
                        "chat.tool_truncated_apology",
                        operator_primary_language,
                    )
                    continue
                # Retry exhausted OR plain text reply — stop the loop.
                # If it was a malformed tool-call attempt and we've
                # already retried once, stick with the polite
                # placeholder rather than leaking the raw JSON.
                if looks_like_tool_call_attempt(text):
                    _LOGGER.warning(
                        "chat tool-use: malformed tool call retries "
                        "exhausted — giving up, hop=%d",
                        hop,
                    )
                    last_text = localized_fallback_text(
                        "chat.tool_truncated_apology",
                        operator_primary_language,
                    )
                break
            if forced_pending:
                # Consume the forced flag: the call is about to run
                # (or already ran via fallback). Next hops use the
                # normal framing so the model can write its wrap-up.
                forced_pending = False
            if call.name == _FORCED_IMAGE_TOOL_NAME and image_tool_executed:
                _LOGGER.warning(
                    "chat tool-use: duplicate image tool call blocked, "
                    "hop=%d",
                    hop,
                )
                tool_outcomes.append(
                    ToolOutcomeMessage(
                        tool_name=call.name,
                        ok=False,
                        output_text="",
                        error=(
                            "本回合已經執行過 generate_image；"
                            "不要再次呼叫生圖工具，請直接用角色台詞回覆。"
                        ),
                    ),
                )
                continue
            if call.name == _FORCED_IMAGE_TOOL_NAME:
                quota_error = await self._reserve_runtime_chat_image_quota(
                    character=character,
                    now=now,
                )
                if quota_error is not None:
                    tool_outcomes.append(
                        ToolOutcomeMessage(
                            tool_name=call.name,
                            ok=False,
                            output_text="",
                            error=quota_error,
                        ),
                    )
                    continue
            # Guard against a stuck model re-emitting the exact same
            # tool call in a loop. If we've already run this call this
            # turn, don't run it again — break out with whatever text
            # the model produced so the loop terminates instead of
            # draining the hop budget on identical invocations.
            try:
                import json as _json
                call_key = (call.name, _json.dumps(call.arguments, sort_keys=True, ensure_ascii=False))
            except Exception:
                call_key = (call.name, repr(call.arguments))
            if call_key in seen_calls:
                _LOGGER.warning(
                    "chat tool-use: duplicate tool call detected, "
                    "breaking loop — tool=%s hop=%d",
                    call.name, hop,
                )
                break
            seen_calls.add(call_key)
            try:
                invocation, result = await self._tool_orchestrator.execute(
                    character=character, call=call,
                    conversation_id=conversation.id,
                    recent_dialogue=recent_dialogue,
                    user_attachment_urls=resolved_user_attachment_urls,
                )
            except Exception:
                _LOGGER.exception("chat tool-use: orchestrator crashed")
                tool_outcomes.append(
                    ToolOutcomeMessage(
                        tool_name=call.name, ok=False,
                        output_text="", error="orchestrator crashed",
                    ),
                )
                continue
            if call.name == _FORCED_IMAGE_TOOL_NAME:
                image_tool_executed = True
            tool_outcomes.append(
                ToolOutcomeMessage(
                    tool_name=call.name,
                    ok=result.ok,
                    output_text=result.output_text,
                    attachment_urls=tuple(a.url for a in result.attachments),
                    error=result.error,
                ),
            )
            if result.ok:
                for att in result.attachments:
                    collected.append(_tool_to_message_attachment(att))
        novelty_retry_count = 0
        novelty_verdict = None
        if self._reply_quality_gate_required(
            register_profile=register_profile,
            diversity_evidence=diversity_evidence,
        ):
            novelty_verdict = await self._evaluate_novelty_gate(
                character=character,
                operator=operator,
                response_text=last_text,
                material_digest=material_digest,
                emotion_events=emotion_events,
                self_reflections=self_reflections,
                story_events=story_events,
                story_arc=story_arc,
                upcoming_arc_beats=upcoming_arc_beats,
                recent_feed_posts=recent_feed_posts,
                recent_messages=recent_messages,
                self_repetition_hint=self_repetition_hint,
                latest_user_message=latest_user_message,
                content_tolerance=content_tolerance,
                register_profile=register_profile,
                diversity_evidence=diversity_evidence,
                persona_context=tuple([
                    f"性格：{', '.join(character.personality)}",
                    f"說話風格：{character.speaking_style}",
                    *initial_relationship_lines,
                ]),
            )

        async def generate_tool_retry_once(
            retry_directive: str,
        ) -> tuple[str, ChatGenerationTrace]:
            prompt = self._prompt_context_builder.build(
                character=character,
                conversation=conversation,
                recent_messages=recent_messages,
                memories=memories,
                pending_state=pending_state,
                latest_user_message=latest_user_message,
                active_goals=active_goals,
                current_activity=current_activity,
                upcoming_activities=upcoming_activities,
                just_finished_activity=just_finished_activity,
                completed_today_activities=completed_today_activities,
                pending_invite_activities=pending_invite_activities,
                now=now,
                idle_minutes=idle_minutes,
                available_tools=[],
                tool_outcomes=tool_outcomes,
                forced_tool_name=None,
                story_events=story_events,
                story_arc=story_arc,
                upcoming_arc_beats=upcoming_arc_beats,
                today_local=today_local,
                older_dialogue_summary=older_dialogue_summary,
                vision_markers=vision_markers,
                image_recognition_context=image_recognition_context,
                recent_proactive_messages=recent_proactive_messages,
                recent_feed_posts=recent_feed_posts,
                self_repetition_hint=self_repetition_hint,
                phrase_habit_lines=phrase_habit_lines,
                operator=operator,
                operator_persona_lines=operator_persona_lines,
                peer_roster_lines=peer_roster_lines,
                initial_relationship_lines=initial_relationship_lines,
                persona_curiosity_plan=persona_curiosity_plan,
                calendar_context=calendar_context,
                weather_context=weather_context,
                world_event_context=world_event_context,
                upcoming_day_schedules=upcoming_day_schedules,
                emotion_events=emotion_events,
                self_reflections=self_reflections,
                address_preference=address_preference,
                resolved_player_address=resolved_player_address,
                resolved_character_address=resolved_character_address,
                address_change_lines=address_change_lines,
                experiment_overlay=experiment_overlay,
                presence_frame=presence_frame,
                content_tolerance=content_tolerance,
                material_digest=material_digest,
                turn_register_profile=register_profile,
                reply_diversity_evidence=diversity_evidence,
                retry_directive=retry_directive,
            )
            prompt_pack_hash = _last_prompt_pack_hash(self._prompt_context_builder)
            prompt_for_model, image_urls = await _prepare_vision_prompt(
                model=model,
                prompt=prompt,
                attachment_urls=vision_urls,
                public_base_url=self._public_base_url,
                uploads_dir=self._uploads_dir,
                object_storage=self._object_storage,
                image_context=image_recognition_context,
            )
            try:
                retry_text, trace = await _generate_capturing(
                    model,
                    prompt_for_model,
                    image_urls=image_urls,
                    model_id=model_id,
                )
            except Exception as exc:
                failed_trace = _failed_generation_trace(
                    prompt=prompt_for_model,
                    prompt_pack_hash=prompt_pack_hash,
                    model_id=(
                        model_id
                        or str(getattr(model, "provider_id", "") or "")
                    ),
                    error=exc,
                )
                await self._record_llm_usage_safely(
                    character_id=character.id,
                    operator_id=owner_user_id,
                    conversation_id=conversation.id,
                    turn_record_id=None,
                    trace=failed_trace,
                    provider_id=str(getattr(model, "provider_id", "") or ""),
                    source_surface=source_surface,
                    upstream_request_id=str(
                        getattr(model, "last_request_id", "") or "",
                    ),
                )
                raise
            return retry_text, replace(trace, prompt_pack_hash=prompt_pack_hash)

        if (
            novelty_verdict is not None
            and not novelty_verdict.passes
            and self._novelty_gate_max_retries > 0
        ):
            novelty_retry_count = 1
            last_text, retry_trace = await generate_tool_retry_once(
                novelty_verdict.feedback,
            )
            traces.append(retry_trace)
        return ChatGenerationResult(
            text=last_text,
            attachments=collected,
            forced_fired=forced_fired,
            trace=_merge_traces(traces),
            persona_curiosity_plan=persona_curiosity_plan,
            material_digest=material_digest,
            register_profile=register_profile,
            diversity_evidence=diversity_evidence,
            novelty_verdict=novelty_verdict,
            novelty_retry_count=novelty_retry_count,
        )

    async def _reserve_runtime_chat_image_quota(
        self,
        *,
        character: Character,
        now: datetime,
    ) -> str | None:
        profile = await self._account_runtime_profile_resolver.resolve_for_operator(
            character.user_id,
        )
        limit = profile.daily_chat_image_limit
        if limit is None:
            return None
        if self._account_runtime_usage_repository is None:
            _LOGGER.error(
                "chat image runtime quota ledger is not configured "
                "(operator=%s)",
                character.user_id,
            )
            return (
                "account runtime profile chat image quota ledger is not "
                "configured"
            )
        try:
            used = await self._account_runtime_usage_repository.count_events(
                operator_id=character.user_id,
                event_type=ACCOUNT_RUNTIME_EVENT_CHAT_IMAGE,
                since=now - timedelta(hours=24),
                until=now,
            )
            if used >= limit:
                return (
                    "account runtime profile daily chat image limit reached "
                    f"({limit}/24h)"
                )
            await self._account_runtime_usage_repository.record_event(
                operator_id=character.user_id,
                event_type=ACCOUNT_RUNTIME_EVENT_CHAT_IMAGE,
                occurred_at=now,
            )
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "chat image runtime quota check failed (operator=%s)",
                character.user_id,
            )
            return "account runtime profile chat image quota is unavailable"
        return None

    async def _ensure_runtime_message_session_available(
        self,
        *,
        character: Character,
        conversation: Conversation,
    ) -> None:
        profile = await self._account_runtime_profile_resolver.resolve_for_operator(
            character.user_id,
        )
        limit = profile.max_messages_per_session
        if limit is None:
            return
        used = sum(
            1
            for message in conversation.messages
            if message.role == MessageRole.USER
        )
        if used >= limit:
            raise ChatRuntimeLimitExceeded(
                "account runtime profile session message limit reached "
                f"({limit}/session)"
            )

    async def _ensure_story_events(
        self,
        character: Character,
        *,
        now: datetime,
    ) -> list[StoryEvent]:
        """Ensure today's story events exist and return them for prompt use.

        Idempotent: same day = cached. Exceptions are swallowed — the
        story gacha is enrichment, not critical path.
        """
        if self._story_event_service is None:
            return []
        try:
            report = await self._story_event_service.ensure_today(
                character, now=now,
            )
            return list(report.events)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("story event ensure_today failed; continuing without")
            return []

    async def _load_upcoming_day_schedules(
        self, character_id: str, *, today_local: date,
    ) -> list:
        """Read already-pre-planned tomorrow + day-after schedules.

        Read-only: the proactive tick is the eager generator
        (``ensure_window``); chat just renders what's there so it
        never pays planner cost on the hot path. Returns an empty
        list when no schedule service is wired, the stub doesn't
        implement the loader, or no days have been pre-planned yet
        — the prompt builder still emits the "remote future is vague"
        rail so the model has a stable answer for "下禮拜要幹嘛".
        """
        if self._schedule_service is None:
            return []
        loader = getattr(
            self._schedule_service, "load_upcoming_schedules", None,
        )
        if loader is None:
            return []
        try:
            return await loader(character_id, start_after=today_local)
        except Exception:
            _LOGGER.exception(
                "chat: load_upcoming_schedules failed character=%s",
                character_id,
            )
            return []

    def _describe_calendar(
        self,
        today_local: date,
        *,
        operator: OperatorProfile | None = None,
    ) -> str:
        """Pull today's real-world calendar block via the schedule service.

        Returns an empty string when no schedule service is wired or
        the schedule-service stand-in (test stub) does not implement
        ``describe_calendar`` — the prompt builder then renders nothing
        for the calendar section. Failures inside the service are
        already logged and swallowed there, so this is a pure
        pass-through.
        """
        if self._schedule_service is None:
            return ""
        describe = getattr(self._schedule_service, "describe_calendar", None)
        if describe is None:
            return ""
        if _accepts_keyword(describe, "operator"):
            return describe(today_local, operator=operator)
        return describe(today_local)

    async def _describe_weather(
        self,
        today_local: date,
        *,
        operator: OperatorProfile | None = None,
    ) -> str:
        """Pull current weather block via the schedule service.

        Async counterpart to :meth:`_describe_calendar` — same façade,
        but :meth:`ScheduleService.describe_weather` is async (HTTP-
        backed Open-Meteo adapter). Returns empty string when no
        schedule service is wired or the stand-in doesn't implement
        ``describe_weather`` so test harnesses don't have to update.
        """
        if self._schedule_service is None:
            return ""
        describe = getattr(self._schedule_service, "describe_weather", None)
        if describe is None:
            return ""
        try:
            if _accepts_keyword(describe, "operator"):
                return await describe(today_local, operator=operator)
            return await describe(today_local)
        except Exception:
            _LOGGER.exception(
                "chat: weather describe failed; falling back to empty",
            )
            return ""

    async def _load_world_event_context(
        self,
        character: Character,
        operator: OperatorProfile | None,
    ) -> tuple[str, ...]:
        if self._event_seed_dispenser is None:
            return ()
        if not getattr(character, "world_awareness_enabled", False):
            return ()
        try:
            seeds = await self._event_seed_dispenser.peek(
                character_id=character.id,
                limit=3,
            )
        except Exception:
            _LOGGER.exception(
                "chat: world-event peek failed character=%s", character.id,
            )
            return ()
        if not seeds:
            return ()
        lines: list[str] = []
        location = prompt_location_fact(operator)
        if location:
            lines.append(f"- {location}")
        for seed in seeds:
            event = seed.event
            title = (event.title or "").strip()
            if not title:
                continue
            parts = [f"標題：{_clip(title, 160)}"]
            source = (event.source or "").strip()
            if source:
                parts.append(f"來源：{_clip(source, 80)}")
            locale = (event.locale or "").strip()
            if locale:
                parts.append(f"來源地區：{_clip(locale, 40)}")
            summary = (event.summary or "").strip()
            if summary:
                parts.append(f"摘要：{_clip(summary, 240)}")
            lines.append("- " + "；".join(parts))
        return tuple(lines)

    async def _ensure_story_arc(
        self,
        character: Character,
        *,
        today: date | None = None,
    ) -> tuple[StoryArc | None, list[StoryArcBeat]]:
        """Fetch / lazy-create the character's active arc for prompt use.

        Returns ``(arc, forward_beats)`` — the active arc (or ``None``
        if the service is disabled / auto_start fails) plus the next
        1–2 upcoming beats so the prompt builder can forward-feed
        anticipation to the model. Failure is silent — arcs are
        narrative colour, not critical path.
        """
        if self._story_arc_service is None:
            return None, []
        if today is None and self._schedule_service is not None:
            resolver = getattr(self._schedule_service, "today_for_character", None)
            if resolver is not None:
                try:
                    today = await resolver(character)
                except Exception:
                    _LOGGER.exception(
                        "chat: owner-local today lookup failed character=%s",
                        character.id,
                    )
        try:
            arc = await self._story_arc_service.ensure_active_arc(
                character,
                today=today,
                auto_start=True,
                open_new_season=False,
            )
            if arc is None:
                return None, []
            anchor = today if today is not None else arc.start_date
            forward = arc.forward_beats(
                after=anchor, limit=2, include_today=False,
            )
            return arc, forward
        except Exception:  # noqa: BLE001
            _LOGGER.exception(
                "story arc ensure_active_arc failed; continuing without",
            )
            return None, []

    async def _embed_query(self, query_text: str | None) -> list[float] | None:
        if self._embedder is None or not query_text:
            return None
        try:
            vector = await self._embedder.embed(query_text)
        except Exception:
            _LOGGER.exception("Query embedding failed")
            return None
        return list(vector) if vector is not None else None

    async def _load_active_goals(self, character_id: str) -> list[CharacterGoal]:
        if self._goal_service is None:
            return []
        try:
            return await self._goal_service.list_active_goals(character_id)
        except Exception:
            _LOGGER.exception("Failed to load active goals")
            return []

    async def _load_recent_proactive_messages(
        self, character_id: str,
    ) -> tuple[ProactiveAttempt, ...]:
        """Mirror of ProactiveDispatcher._load_recent_sent_attempts.

        Surfaces the last few messages the character pushed unprompted
        (across surfaces — Telegram / LINE / web) to the chat prompt,
        so the reply LLM doesn't ask the same question the proactive
        push just asked. Best-effort: any failure → empty tuple.
        """
        if self._proactive_attempt_repository is None:
            return ()
        try:
            sent = await self._proactive_attempt_repository.list_recent_sent(
                character_id, limit=3,
            )
        except Exception:
            _LOGGER.exception("chat: proactive attempt log query failed")
            return ()
        return tuple(sent)

    async def _load_recent_feed_posts(
        self, character_id: str,
    ) -> tuple[FeedPost, ...]:
        """Surface the character's own most-recent feed-wall posts.

        Closes the cross-surface identity gap: without this rail the
        chat-side LLM has no idea the character just posted on the feed
        wall, so when the user opens chat with "你那篇咖啡的動態怎麼了"
        the character looks blank. We pull the freshest 3 posts so the
        prompt builder can render them under the "你最近在動態牆發過"
        block. Best-effort: any failure → empty tuple.
        """
        if self._feed_post_repository is None:
            return ()
        try:
            recent = await self._feed_post_repository.list_for_character(
                character_id, limit=3,
            )
        except Exception:
            _LOGGER.exception("chat: feed post query failed")
            return ()
        return tuple(recent)

    async def _prepare_prompt_dialogue_context(
        self,
        *,
        character: Character,
        recent_messages: list[Message],
        content_tolerance: str = CONTENT_TOLERANCE_FRONTIER,
    ) -> tuple[list[Message], str]:
        """Return ``(raw_tail, older_summary)`` for prompt composition.

        Policy:
        - Keep the latest ``_PROMPT_RAW_RECENT_MESSAGE_LIMIT`` turns raw.
        - Older turns are condensed through ``DialogueSummarizerPort``.
        - Summary unavailable/fails/empty => keep the original raw list
          (fail-soft, no context loss).
        """
        if len(recent_messages) <= _PROMPT_RAW_RECENT_MESSAGE_LIMIT:
            return recent_messages, ""
        raw_tail = recent_messages[-_PROMPT_RAW_RECENT_MESSAGE_LIMIT:]
        older = recent_messages[:-_PROMPT_RAW_RECENT_MESSAGE_LIMIT]
        older_for_summary = sanitize_messages_for_tolerance(
            older,
            content_tolerance=content_tolerance,
        )
        summary = await self._summarize_older_dialogue(
            character=character,
            messages=older_for_summary,
        )
        if summary:
            return raw_tail, summary
        if (
            normalize_content_tolerance(content_tolerance)
            == CONTENT_TOLERANCE_FRONTIER
            and contains_restricted_messages(older)
        ):
            return raw_tail, ""
        return recent_messages, ""

    async def _summarize_older_dialogue(
        self,
        *,
        character: Character,
        messages: list[Message],
    ) -> str:
        if self._dialogue_summarizer is None or not messages:
            return ""
        # ``TOOL_ONLY`` carries artifact URLs / empty text and pollutes
        # the summary. Keep only meaningful dialogue lines.
        filtered = [
            message
            for message in messages
            if message.kind is not MessageKind.TOOL_ONLY
            and (message.content or "").strip()
        ]
        if len(filtered) < 2:
            return ""
        try:
            return (await self._dialogue_summarizer.summarize(
                character=character,
                messages=filtered,
            )).strip()
        except Exception:
            _LOGGER.exception(
                "chat prompt older-dialogue summarise failed character=%s",
                character.id,
            )
            return ""

    async def _load_current_schedule(
        self,
        character: Character,
        *,
        now: datetime | None = None,
        local_tz: tzinfo | None = None,
    ) -> tuple[
        ScheduleActivity | None,
        list[ScheduleActivity],
        ScheduleActivity | None,
        list[ScheduleActivity],
        list[ScheduleActivity],
    ]:
        if self._schedule_service is None:
            return None, [], None, [], []
        try:
            schedule = await self._schedule_service.ensure_schedule(character)
        except Exception:
            _LOGGER.exception("Failed to ensure daily schedule")
            return None, [], None, [], []
        # Fire-and-forget memorialization of any completed activities —
        # converts past schedule blocks into episodic memories. Idempotent
        # thanks to the ``memorialized`` flag, so running every turn is
        # safe and cheap once the flag is set.
        self._maybe_memorialize(character.id)
        try:
            moment = now or self._resolve_now()
            current, upcoming, just_finished = (
                self._schedule_service.resolve_current(schedule, now=moment)
            )
            completed_today = self._schedule_service.resolve_completed_today(
                schedule,
                now=moment,
                local_tz=local_tz,
            )
            pending_window = [schedule]
            pending_window.extend(
                await self._load_upcoming_day_schedules(
                    character.id,
                    today_local=schedule.date,
                )
            )
            pending_invites = (
                self._schedule_service.resolve_pending_invites_from_schedules(
                    pending_window,
                    now=moment,
                    limit=1,
                )
            )
        except Exception:
            _LOGGER.exception("Failed to resolve current activity")
            return None, [], None, [], []
        return current, upcoming, just_finished, completed_today, pending_invites

    def _maybe_memorialize(self, character_id: str) -> None:
        if self._schedule_memorializer is None:
            return
        coro = self._do_memorialize(character_id)
        # Always schedule in background — memorialization is purely
        # bookkeeping and must never block the reply.
        self._schedule_background(coro)

    async def _do_memorialize(self, character_id: str) -> None:
        if self._schedule_memorializer is None:
            return
        try:
            await self._schedule_memorializer.memorialize(character_id=character_id)
        except Exception:
            _LOGGER.exception("Schedule memorialization crashed")

    async def _memorialize_feed_reactions(self, character_id: str) -> None:
        # Run inline (not background) so the freshly-written memories are
        # visible to ``_select_memories`` on the same turn — that's what
        # lets the character thank the user for a comment they just left.
        # Fail-soft: a memorializer crash must never block chat reply.
        if self._feed_reaction_memorializer is None:
            return
        try:
            await self._feed_reaction_memorializer.memorialize(
                character_id=character_id,
            )
        except Exception:
            _LOGGER.exception("Feed reaction memorialization crashed")

    async def _touch_memories(self, memories: list[MemoryItem]) -> None:
        for memory in memories:
            await self._memory_repository.touch(memory.id)

    async def _load_self_reflections(
        self,
        *,
        character_id: str,
        operator: "OperatorProfile | None",
    ) -> list:
        """HUMANIZATION_ROADMAP §3.2 — pull the latest reflection rows for
        the (character, operator) pair to fold into the prompt.

        Returns ``[]`` whenever the repo is not wired, the operator is
        unknown, or the lookup raises — chat path must never break on
        this auxiliary signal.
        """
        if self._self_reflection_repository is None or operator is None:
            return []
        try:
            return await self._self_reflection_repository.latest_for(
                character_id, operator.id,
            )
        except Exception:
            _LOGGER.exception(
                "self_reflection lookup failed character=%s",
                character_id,
            )
            return []

    async def _load_operator_persona(
        self,
        character_id: str,
        operator: "OperatorProfile | None",
    ):
        """Fetch this character's persona aggregate (or ``None``).

        Per-character: only this character's accumulated observations
        load — siblings' confirmed facts are invisible. Returns ``None``
        when the persona service isn't wired or the operator is
        unresolved. Failure-soft: a broken persona service must not
        break the chat path. The caller renders the lines *and* feeds
        the same entity to the address resolver, so the persona is
        fetched once per turn rather than twice.
        """
        if self._operator_persona_service is None or operator is None:
            return None
        try:
            return await self._operator_persona_service.get_current(
                character_id, operator.id,
            )
        except Exception:
            _LOGGER.exception("operator persona load failed; continuing without it")
            return None

    def _render_operator_persona_lines(self, persona) -> list[str]:
        """Render the operator-persona prompt block from a loaded
        aggregate. The prompt builder splices the result in
        unconditionally, so an empty list collapses cleanly."""
        if persona is None or self._operator_persona_service is None:
            return []
        try:
            return self._operator_persona_service.render_for_prompt(persona)
        except Exception:
            _LOGGER.exception(
                "operator persona render failed; rendering without it",
            )
            return []

    async def _load_peer_roster_lines(self, character_id: str) -> list[str]:
        if self._character_social_knowledge_service is None:
            return []
        try:
            return await self._character_social_knowledge_service.render_roster_for_prompt(
                character_id,
            )
        except Exception:
            _LOGGER.exception(
                "character peer roster render failed; rendering without it",
            )
            return []

    async def _load_peer_context_lines_for_extraction(
        self,
        character_id: str,
    ) -> list[str]:
        if self._character_social_knowledge_service is None:
            return []
        try:
            return (
                await self._character_social_knowledge_service
                .render_known_peers_for_extraction(character_id)
            )
        except Exception:
            _LOGGER.exception(
                "peer context render failed; post-turn extraction continues without it",
            )
            return []

    async def _load_initial_relationship_lines(
        self,
        character_id: str,
        operator: "OperatorProfile | None",
    ) -> list[str]:
        if self._relationship_seed_repository is None or operator is None:
            return []
        try:
            seed = await self._relationship_seed_repository.get(
                character_id, operator.id,
            )
            return render_initial_relationship_seed_lines(seed)
        except Exception:
            _LOGGER.exception(
                "initial relationship seed render failed; rendering without it",
            )
            return []

    async def _resolve_addresses(
        self,
        character: "Character",
        operator: "OperatorProfile | None",
        address_preference,
        persona=None,
    ) -> tuple[ResolvedAddress, ResolvedAddress]:
        """Resolve both address directions for the chat prompt.

        Uses the already-loaded ``persona`` (fetched once per turn by the
        caller) plus a per-character seed so the rendered 「稱呼」 follows
        seed > persona > global display name, instead of the raw display
        name (a platform/OAuth label — sometimes a UUID — that reads
        badly in-fiction). Failure-soft: a lookup error degrades to the
        lower-precedence sources rather than breaking the chat path.
        """
        seed = None
        if operator is not None and self._relationship_seed_repository is not None:
            try:
                seed = await self._relationship_seed_repository.get(
                    character.id, operator.id,
                )
            except Exception:
                _LOGGER.exception("address resolve: seed lookup failed")
        player = resolve_player_address(seed=seed, persona=persona, profile=operator)
        character_direction = resolve_character_address(
            seed=seed, preference=address_preference, character=character,
        )
        return player, character_direction

    async def _load_address_change_lines(
        self,
        character: "Character",
        operator: "OperatorProfile | None",
    ) -> list[str]:
        """Render the latest per-pair rename (one event per direction) as
        relationship-event lines for the chat prompt.

        History is never rewritten on a rename — old memories keep the old
        name. Surfacing the most recent change lets the character
        acknowledge the new term and link older references to the same
        person. Returns ``[]`` when the log isn't wired, the operator is
        unresolved, or no rename exists; fail-soft on lookup error."""
        if self._address_change_log_repository is None or operator is None:
            return []
        try:
            player_event = await self._address_change_log_repository.latest(
                character_id=character.id,
                operator_id=operator.id,
                direction=DIRECTION_PLAYER,
            )
            character_event = await self._address_change_log_repository.latest(
                character_id=character.id,
                operator_id=operator.id,
                direction=DIRECTION_CHARACTER,
            )
        except Exception:
            _LOGGER.exception("address change log lookup failed")
            return []
        return render_address_change_lines(
            player_event=player_event,
            character_event=character_event,
            local_tz=_operator_timezone(operator),
        )

    async def _resolve_player_address_for_aux(
        self,
        character: "Character",
        operator: "OperatorProfile | None",
    ) -> "ResolvedAddress | None":
        """Resolve direction-A (character → player) address for the
        post-turn auxiliary passes (memory-content naming, persona
        extraction) that run after the reply.

        Uses the explicit per-character seed over the global display name
        so a seed name (not a raw platform/OAuth label) names the operator
        in *stored memories* too — not just the live prompt. The learned
        persona name is deliberately *not* fetched here: these passes run
        after the reply, the per-turn persona is already fetched once on
        the chat-build path (and again by persona extraction for its own
        prompt), and re-fetching it just to refine a memory label would
        break that single-fetch invariant for marginal benefit. Fail-soft:
        a seed lookup error degrades to the global profile."""
        if operator is None:
            return None
        seed = None
        if self._relationship_seed_repository is not None:
            try:
                seed = await self._relationship_seed_repository.get(
                    character.id, operator.id,
                )
            except Exception:
                _LOGGER.exception("aux address resolve: seed lookup failed")
        return resolve_player_address(
            seed=seed, persona=None, profile=operator,
        )

    async def _load_busy_interaction_lines(
        self,
        character_id: str,
        operator: "OperatorProfile | None",
        *,
        relationship_context_lines: list[str] | tuple[str, ...] = (),
    ) -> list[str]:
        if self._operator_persona_service is None or operator is None:
            return []
        try:
            strength = await self._operator_persona_service.get_interaction_strength(
                character_id, operator.id,
            )
            lines = _render_busy_interaction_lines(strength)
            if any(line.strip() for line in relationship_context_lines):
                lines.append(
                    "- 忙碌判斷邊界：起始關係設定是關係主述；"
                    "互動量低不可覆寫這份關係或要求角色裝陌生。",
                )
            return lines
        except Exception:
            _LOGGER.exception(
                "busy-defer interaction context failed; rendering without it",
            )
            return []

    async def _load_phrase_habit_lines(self, character_id: str) -> list[str]:
        """Load observed speech habits for chat prompt style guidance.

        Schedule-shaped behavioral patterns stay in ScheduleService. Chat
        only receives ``phrase_habit`` rows so the model can preserve
        a character's emerging voice without treating activity patterns
        as dialogue style.
        """
        if self._behavioral_patterns is None:
            return []
        try:
            patterns = await self._behavioral_patterns.list_for_character(
                character_id,
                kinds=(KIND_PHRASE_HABIT,),
                limit=3,
            )
        except Exception:
            _LOGGER.exception(
                "phrase habit lookup failed; rendering without it",
            )
            return []
        return [
            pattern.description.strip()
            for pattern in patterns
            if pattern.description.strip()
        ]

    async def _load_prompt_material_digest(
        self,
        *,
        character: Character,
        operator: "OperatorProfile | None",
        emotion_events: list[EmotionEvent],
        self_reflections: list,
        story_events: list[StoryEvent] | None,
        story_arc: "StoryArc | None",
        upcoming_arc_beats: "list[StoryArcBeat] | None",
        recent_feed_posts: tuple[FeedPost, ...],
        content_tolerance: str,
    ) -> PromptMaterialDigest | None:
        if (
            not self._prompt_material_digest_enabled
            or self._prompt_material_digester is None
        ):
            return None
        operator_id = getattr(operator, "id", None) or getattr(
            character,
            "user_id",
            DEFAULT_OPERATOR_ID,
        )
        context = PromptMaterialDigestContext(
            character_id=character.id,
            operator_id=operator_id,
            emotion_events=tuple(_digest_emotion_lines(emotion_events)),
            self_reflections=tuple(_digest_reflection_lines(self_reflections)),
            story_events=tuple(_digest_story_event_lines(story_events or [])),
            story_arc=tuple(_digest_story_arc_lines(story_arc, upcoming_arc_beats or [])),
            recent_feed_posts=tuple(_digest_feed_lines(recent_feed_posts)),
            source_language=getattr(operator, "primary_language", "") or "",
            content_tolerance=content_tolerance,
        )
        try:
            return await self._prompt_material_digester.digest(
                context,
                character=character,
            )
        except Exception:
            _LOGGER.exception(
                "prompt material digest failed; rendering source blocks",
            )
            return None

    async def _load_register_profile(
        self,
        *,
        character: Character,
        operator: "OperatorProfile | None",
        latest_user_message: str,
        recent_dialogue_summary: str,
        relationship_context: tuple[str, ...],
        content_tolerance: str,
    ) -> RegisterProfile | None:
        if (
            not self._register_profile_enabled
            or self._register_profiler is None
        ):
            return None
        operator_id = getattr(operator, "id", None) or getattr(
            character,
            "user_id",
            DEFAULT_OPERATOR_ID,
        )
        context = RegisterProfileContext(
            character_id=character.id,
            operator_id=operator_id,
            latest_user_message=latest_user_message,
            recent_dialogue_summary=recent_dialogue_summary,
            relationship_context=relationship_context,
            content_tolerance=content_tolerance,
        )
        try:
            return await self._register_profiler.profile(
                context,
                character=character,
            )
        except Exception:
            _LOGGER.exception("register profiler failed open")
            return None

    def _reply_quality_gate_required(
        self,
        *,
        register_profile: RegisterProfile | None,
        diversity_evidence: ReplyDiversityEvidence | None,
    ) -> bool:
        if not self._novelty_gate_enabled or self._novelty_gate is None:
            return False
        if self._reply_quality_gate_risk_threshold <= 0.0:
            return True
        score = _reply_quality_risk_score(
            register_profile=register_profile,
            diversity_evidence=diversity_evidence,
            similarity_threshold=self._reply_quality_similarity_threshold,
        )
        return score >= self._reply_quality_gate_risk_threshold

    async def _evaluate_novelty_gate(
        self,
        *,
        character: Character,
        operator: "OperatorProfile | None",
        response_text: str,
        material_digest: PromptMaterialDigest | None,
        emotion_events: list[EmotionEvent],
        self_reflections: list,
        story_events: list[StoryEvent] | None,
        story_arc: "StoryArc | None",
        upcoming_arc_beats: "list[StoryArcBeat] | None",
        recent_feed_posts: tuple[FeedPost, ...],
        recent_messages: list[Message],
        self_repetition_hint: str | None,
        latest_user_message: str,
        content_tolerance: str,
        register_profile: RegisterProfile | None = None,
        diversity_evidence: ReplyDiversityEvidence | None = None,
        persona_context: tuple[str, ...] = (),
    ) -> NoveltyVerdict | None:
        if not self._novelty_gate_enabled or self._novelty_gate is None:
            return None
        operator_id = getattr(operator, "id", None) or getattr(
            character,
            "user_id",
            DEFAULT_OPERATOR_ID,
        )
        context = NoveltyGateContext(
            character_id=character.id,
            operator_id=operator_id,
            response_text=response_text,
            known_material=tuple(_novelty_known_material_lines(
                material_digest=material_digest,
                emotion_events=emotion_events,
                self_reflections=self_reflections,
                story_events=story_events or [],
                story_arc=story_arc,
                upcoming_arc_beats=upcoming_arc_beats or [],
                recent_feed_posts=recent_feed_posts,
            )),
            recent_self_lines=tuple(_recent_assistant_lines(recent_messages)),
            self_repetition_hint=self_repetition_hint or "",
            latest_user_message=latest_user_message,
            content_tolerance=content_tolerance,
            register_profile=register_profile,
            diversity_evidence=diversity_evidence,
            persona_context=persona_context,
        )
        try:
            return await self._novelty_gate.evaluate(context, character=character)
        except Exception as exc:
            _LOGGER.exception("novelty gate failed open")
            return NoveltyVerdict.pass_open(repr(exc))

    async def _load_persona_curiosity_plan(
        self,
        *,
        character: Character,
        operator: "OperatorProfile | None",
        enabled: bool,
        conversation_id: str | None = None,
        recent_dialogue_summary: str = "",
        initial_relationship_lines: list[str] | tuple[str, ...] = (),
        now: datetime | None = None,
    ) -> PersonaCuriosityPlan | None:
        """Build one optional natural persona-discovery writing hint."""
        if (
            not enabled
            or operator is None
            or self._operator_persona_service is None
            or self._persona_curiosity_service is None
            or self._persona_curiosity_planner is None
        ):
            return None
        try:
            persona = await self._operator_persona_service.get_current(
                character.id,
                operator.id,
            )
            context = await self._persona_curiosity_service.build_context(
                persona=persona,
                surface="chat",
                recent_dialogue_summary=recent_dialogue_summary,
                initial_relationship_lines=tuple(initial_relationship_lines),
                now=now,
                operator_primary_language=(
                    getattr(operator, "primary_language", "") or "zh-TW"
                ),
            )
            plan = await self._persona_curiosity_planner.plan(
                context,
                character=character,
            )
        except Exception:
            _LOGGER.exception(
                "persona curiosity plan failed; rendering without it",
            )
            return None
        await self._record_persona_curiosity_planned(
            context=context,
            plan=plan,
            conversation_id=conversation_id,
            now=now,
        )
        return plan

    async def _record_persona_curiosity_planned(
        self,
        *,
        context,
        plan: PersonaCuriosityPlan,
        conversation_id: str | None,
        now: datetime | None,
    ) -> None:
        if self._persona_curiosity_service is None:
            return
        try:
            await self._persona_curiosity_service.record_planned_attempt(
                context=context,
                plan=plan,
                conversation_id=conversation_id,
                now=now,
            )
        except Exception:
            _LOGGER.exception(
                "persona curiosity planned-attempt record failed; continuing",
            )

    async def _run_post_turn(
        self,
        *,
        character: Character,
        conversation_id: str,
        turn_record_id: str,
        user_text: str,
        assistant_text: str,
        prior_messages: list[Message],
        persona_enabled: bool = True,
        content_mode: str = CONTENT_MODE_NORMAL,
    ) -> dict:
        coro = self._do_post_turn(
            character=character,
            conversation_id=conversation_id,
            turn_record_id=turn_record_id,
            user_text=user_text,
            assistant_text=assistant_text,
            prior_messages=prior_messages,
            persona_enabled=persona_enabled,
            content_mode=content_mode,
        )
        if self._extract_in_background:
            self._schedule_background(coro)
            return {"post_turn_background": True}
        else:
            return await coro

    async def _do_post_turn(
        self,
        *,
        character: Character,
        conversation_id: str,
        turn_record_id: str,
        user_text: str,
        assistant_text: str,
        prior_messages: list[Message],
        persona_enabled: bool = True,
        content_mode: str = CONTENT_MODE_NORMAL,
    ) -> dict:
        operator = await self._load_operator(
            user_id=getattr(character, "user_id", DEFAULT_OPERATOR_ID),
        )
        post_turn_started = self._resolve_now()
        today_local = to_timezone(
            post_turn_started, _operator_timezone(operator),
        ).date()
        active_schedule = None
        if self._schedule_service is not None:
            try:
                active_schedule = await self._schedule_service.get_schedule(
                    character.id, date_=today_local,
                )
            except Exception:
                _LOGGER.exception("Failed to load schedule for post-turn context")
                active_schedule = None

        active_arc = None
        if self._story_arc_service is not None:
            try:
                active_arc = await self._story_arc_service.get_active(character.id)
            except Exception:
                _LOGGER.exception("Failed to load arc for post-turn context")
                active_arc = None
        peer_context_lines = await self._load_peer_context_lines_for_extraction(
            character.id,
        )
        # Resolve the player address once for both post-turn passes so a
        # per-character seed name (not the raw display name) names the
        # operator in stored memories and in persona extraction.
        resolved_player_address = await self._resolve_player_address_for_aux(
            character, operator,
        )

        try:
            kwargs = {
                "character": character,
                "conversation_id": conversation_id,
                "user_message": user_text,
                "assistant_message": assistant_text,
                "recent_messages": prior_messages,
                "active_schedule": active_schedule,
                "active_arc": active_arc,
                "operator": operator,
                "now": post_turn_started,
            }
            if _accepts_keyword(self._post_turn_processor.process, "content_mode"):
                kwargs["content_mode"] = content_mode
            if _accepts_keyword(self._post_turn_processor.process, "peer_context_lines"):
                kwargs["peer_context_lines"] = peer_context_lines
            if _accepts_keyword(
                self._post_turn_processor.process, "resolved_player_address",
            ):
                kwargs["resolved_player_address"] = resolved_player_address
            result = await self._post_turn_processor.process(**kwargs)
        except Exception as exc:
            _LOGGER.exception("Post-turn processing crashed")
            await self._record_turn_safely(TurnRecordingDraft(
                character_id=character.id,
                kind="post_turn_processor",
                conversation_id=conversation_id,
                error=repr(exc),
                latency_ms=int(
                    (self._resolve_now() - post_turn_started).total_seconds() * 1000,
                ),
                post_turn_refs={
                    "parent_turn_record_id": turn_record_id,
                    "content_mode": content_mode,
                },
            ))
            return {"post_turn_error": repr(exc)}

        memory_ids: list[str] = []
        emotion_event_ids: list[str] = []
        await self._record_turn_safely(TurnRecordingDraft(
            character_id=character.id,
            kind="post_turn_processor",
            conversation_id=conversation_id,
            latency_ms=int(
                (self._resolve_now() - post_turn_started).total_seconds() * 1000,
            ),
            response_json={
                "memory_count": len(result.memories),
                "has_state_suggestion": result.state_suggestion is not None,
                "emotion_event_count": len(result.emotion_events or ()),
                "schedule_adjustment_count": len(result.schedule_adjustments or ()),
                "arc_adjustment_count": len(result.arc_adjustments or ()),
                "peer_meet_intent_count": len(result.peer_meet_intents or ()),
            },
            post_turn_refs={
                "parent_turn_record_id": turn_record_id,
                "content_mode": content_mode,
            },
        ))

        # Deduplicate new memories against existing ones, then attach
        # embeddings so semantic retrieval picks them up from the next
        # turn onwards. Fail-loud policy: if the operational embedder
        # can't produce vectors we refuse to persist the memories —
        # silently writing embedding-less rows would poison semantic
        # retrieval and is worse than losing a few memories we can
        # regenerate from the conversation log.
        if result.memories:
            existing = await self._memory_repository.query(
                character.id,
                limit=_MEMORY_POOL_SIZE,
                world_scope=None,
            )
            memories = _with_nsfw_memory_tags(result.memories, content_mode)
            unique = deduplicate(memories, existing)
            if unique:
                try:
                    embedded = await attach_embeddings(unique, self._embedder)
                except EmbedderError:
                    _LOGGER.exception(
                        "Embedder unavailable; skipping persistence of %d memory item(s)",
                        len(unique),
                    )
                else:
                    await self._memory_repository.add_many(embedded)
                    memory_ids.extend(item.id for item in embedded)
                    self._maybe_auto_consolidate(character.id)

        # Phase 3 event-sourcing: prefer the LLM's explicit
        # ``emotion_events`` array (richer — has evidence_quote, valence,
        # arousal, per-event half-life). Fall back to the legacy
        # ``state_suggestion`` mirror when the LLM didn't emit any.
        # This is intentionally independent from ``state_suggestion``:
        # a model may decide there is an emotional event worth auditing
        # without changing the flat state columns on that exact turn.
        if result.emotion_events:
            emotion_event_ids.extend(
                await self._record_emotion_event_candidates(
                    character=character,
                    cause_ref_id=turn_record_id,
                    operator=operator,
                    candidates=result.emotion_events,
                ),
            )
        elif result.state_suggestion is not None:
            emotion_event_ids.extend(
                await self._record_emotion_event_from_state_suggestion(
                    character=character,
                    cause_ref_id=turn_record_id,
                    operator=operator,
                    suggestion=result.state_suggestion,
                    assistant_text=assistant_text,
                ),
            )

        # Apply LLM-refined state (emotion + deltas + current_intent)
        if result.state_suggestion is not None:
            if self._emotion_event_repository is not None and emotion_event_ids:
                await self._apply_state_suggestion_compat(
                    character.id, result.state_suggestion,
                )
            else:
                await self._apply_state_suggestion(
                    character.id, result.state_suggestion,
                )

        # Apply LLM-proposed schedule mutations (Phase 2.3).
        if result.schedule_adjustments and self._schedule_service is not None:
            try:
                await self._schedule_service.apply_adjustments(
                    character_id=character.id,
                    adjustments=result.schedule_adjustments,
                    character=character,
                )
            except Exception:
                _LOGGER.exception("Failed to apply schedule adjustments")

        # Apply LLM-proposed arc mutations — translate post-turn signals
        # into the service-level ``ArcAdjustment`` shape. Same tolerance
        # policy as schedule adjustments (unknown ids silently dropped
        # downstream).
        if result.arc_adjustments and self._story_arc_service is not None:
            try:
                from kokoro_link.application.services.story_arc_service import (
                    ArcAdjustment,
                )
                translated = []
                for sig in result.arc_adjustments:
                    if sig.action == "mark_realized" and not sig.narrative:
                        continue
                    if (
                        sig.action == "mark_realized"
                        and sig.beat_id
                        and sig.narrative
                        and self._story_event_service is not None
                    ):
                        event = await (
                            self._story_event_service
                            .record_arc_beat_realization(
                                character,
                                beat_id=sig.beat_id,
                                narrative=sig.narrative,
                                now=post_turn_started,
                            )
                        )
                        if event is not None:
                            continue
                    translated.append(
                        ArcAdjustment(
                            action=sig.action,
                            beat_id=sig.beat_id,
                            days=sig.days,
                            scheduled_date=sig.scheduled_date,
                            title=sig.title,
                            summary=sig.summary,
                            tension=sig.tension,
                            reason=sig.reason,
                            narrative=sig.narrative,
                        ),
                    )
                if translated:
                    await self._story_arc_service.apply_adjustments(
                        character_id=character.id,
                        adjustments=translated,
                    )
            except Exception:
                _LOGGER.exception("Failed to apply arc adjustments")

        # Persist any "I'll message you at X" promises as scheduled-
        # promise PendingFollowUp rows. These trigger the proactive
        # dispatcher at the promised time and bypass quiet_hours /
        # daily_limit / cooldown / proactive_enabled gates (the user
        # explicitly asked for this push). Same fail-soft policy as
        # other post-turn applications — a write failure must not
        # bring down the turn.
        if (
            result.message_promises
            and self._pending_follow_up_repository is not None
        ):
            try:
                await self._persist_message_promises(
                    character_id=character.id,
                    conversation_id=conversation_id,
                    promises=result.message_promises,
                    operator=operator,
                    content_mode=content_mode,
                )
            except Exception:
                _LOGGER.exception(
                    "Failed to persist message promises character=%s",
                    character.id,
                )

        if (
            result.peer_meet_intents
            and self._character_encounter_intent_repository is not None
        ):
            try:
                await self._persist_peer_meet_intents(
                    character_id=character.id,
                    intents=result.peer_meet_intents,
                    operator=operator,
                )
            except Exception:
                _LOGGER.exception(
                    "Failed to persist peer meet intents character=%s",
                    character.id,
                )

        if result.address_changes:
            await self._apply_observed_address_changes(
                character=character,
                operator=operator,
                changes=result.address_changes,
            )

        # Persona accumulation — per-character (this character builds
        # its own picture of the operator; sibling characters are
        # blind to it). Separate LLM call from post-turn so it
        # doesn't dilute the memory/state pass. Independent of the
        # post-turn result above — even when no memories were
        # extracted the operator may have revealed personal facts
        # worth staging.
        if persona_enabled:
            await self._run_persona_extraction(
                character=character,
                operator=operator,
                conversation_id=conversation_id,
                user_text=user_text,
                assistant_text=assistant_text,
                prior_messages=prior_messages,
                content_mode=content_mode,
                resolved_player_address=resolved_player_address,
            )

        return {
            "memory_ids": memory_ids,
            "emotion_event_ids": emotion_event_ids,
            "state_suggestion_applied": result.state_suggestion is not None,
        }

    async def _run_persona_extraction(
        self,
        *,
        character: Character,
        operator: OperatorProfile | None,
        conversation_id: str,
        user_text: str,
        assistant_text: str,
        prior_messages: list[Message],
        content_mode: MessageContentMode | str = MessageContentMode.NORMAL,
        resolved_player_address: "ResolvedAddress | None" = None,
    ) -> None:
        if (
            self._persona_extraction_service is None
            or operator is None
            or not (user_text and user_text.strip())
        ):
            return
        try:
            user_message_id = (
                f"{conversation_id}#turn-"
                f"{self._resolve_now().isoformat()}"
            )
            # Resolve the player address when the caller didn't already
            # (busy-defer path); the post-turn path passes its resolution
            # so the seed/persona lookup runs once per turn.
            if resolved_player_address is None:
                resolved_player_address = (
                    await self._resolve_player_address_for_aux(character, operator)
                )
            await self._persona_extraction_service.run_after_turn(
                character_id=character.id,
                operator=operator,
                conversation_id=conversation_id,
                user_message_id=user_message_id,
                user_text=user_text,
                assistant_text=assistant_text,
                recent_messages=prior_messages,
                content_mode=content_mode,
                resolved_player_address=resolved_player_address,
            )
        except Exception:
            _LOGGER.exception(
                "Persona extraction trigger crashed character=%s",
                character.id,
            )

    async def _apply_observed_address_changes(
        self,
        *,
        character: Character,
        operator: OperatorProfile | None,
        changes: list,
    ) -> None:
        """Route post-turn-observed address changes (「叫我森森」) through
        the address-change governance (seed update + per-direction rename
        log + persona name reconcile) instead of letting them land as a
        direction-flipped free-text memory.

        Direction ``player`` updates how the character addresses the
        operator (``user_address_name``); ``character`` updates how the
        operator addresses the character (``character_address_name``). The
        rename log is stamped ``observed`` so it is distinguishable from a
        settings-UI edit. Fail-soft and no-op when the service or operator
        is missing."""
        if self._relationship_names_service is None or operator is None:
            return
        character_name = getattr(character, "name", "") or ""
        character_name_key = character_name.strip().casefold()
        for change in changes:
            direction = getattr(change, "direction", "")
            new_value = getattr(change, "new_value", "")
            if not new_value:
                continue
            # A ``player``-direction change whose value is the character's own
            # name is a direction-inversion mis-read: the player is *calling
            # the character* by name, not asking to be addressed by it. Drop
            # it before it can overwrite how the character addresses the
            # player. Case/whitespace-normalised exact match only — no fuzzy /
            # substring matching, to avoid over-blocking legitimate renames.
            if (
                direction == DIRECTION_PLAYER
                and character_name_key
                and new_value.strip().casefold() == character_name_key
            ):
                _LOGGER.info(
                    "observed address change skipped: player-direction value "
                    "%r equals the character's own name (calling the character, "
                    "not renaming the player) character=%s",
                    new_value, character.id,
                )
                continue
            kwargs: dict = {}
            if direction == DIRECTION_PLAYER:
                kwargs["user_address_name"] = new_value
            elif direction == DIRECTION_CHARACTER:
                kwargs["character_address_name"] = new_value
            else:
                continue
            try:
                await self._relationship_names_service.update_names(
                    character_id=character.id,
                    operator_id=operator.id,
                    source=SOURCE_OBSERVED,
                    **kwargs,
                )
            except Exception:
                _LOGGER.exception(
                    "observed address change apply failed character=%s dir=%s",
                    character.id, direction,
                )

    async def _persist_peer_meet_intents(
        self,
        *,
        character_id: str,
        intents: list,
        operator: OperatorProfile | None = None,
    ) -> None:
        from kokoro_link.domain.entities.character_encounter_intent import (
            CharacterEncounterIntent as _CharacterEncounterIntent,
        )

        repo = self._character_encounter_intent_repository
        if repo is None:
            return
        now = self._resolve_now()
        operator_tz = _operator_timezone(operator)
        for intent in intents:
            desired_after = _parse_promise_datetime(
                intent.desired_after_iso,
                local_tz=operator_tz,
            )
            if desired_after is None:
                _LOGGER.info(
                    "peer-meet intent skipped: unparseable iso=%r",
                    intent.desired_after_iso,
                )
                continue
            if desired_after <= now - timedelta(hours=1):
                _LOGGER.info(
                    "peer-meet intent skipped: stale desired_after=%s now=%s",
                    desired_after.isoformat(), now.isoformat(),
                )
                continue
            row = _CharacterEncounterIntent.create(
                character_id=character_id,
                peer_character_id=intent.peer_character_id,
                desired_after=desired_after,
                topic=intent.topic,
                source="chat_agreement",
                source_text=intent.source_text,
                now=now,
            )
            try:
                await repo.add(row)
                _LOGGER.info(
                    "peer-meet intent queued character=%s peer=%s desired_after=%s",
                    character_id,
                    intent.peer_character_id,
                    desired_after.isoformat(),
                )
            except Exception:
                _LOGGER.exception(
                    "peer-meet intent persist failed character=%s peer=%s",
                    character_id,
                    intent.peer_character_id,
                )

    async def _persist_message_promises(
        self,
        *,
        character_id: str,
        conversation_id: str,
        promises: list,
        operator: OperatorProfile | None = None,
        content_mode: MessageContentMode | str = MessageContentMode.NORMAL,
    ) -> None:
        """Write extracted message promises to ``pending_follow_ups``.

        Skipped entries (past-dated, unparseable timezone, repo missing)
        are logged but never raise — the chat turn already succeeded
        and the user shouldn't see a 500 because the LLM emitted a
        slightly malformed promise.
        """
        from kokoro_link.domain.entities.pending_follow_up import (
            PendingFollowUp as _PendingFollowUp,
        )

        repo = self._pending_follow_up_repository
        if repo is None:
            return
        now = self._resolve_now()
        operator_tz = _operator_timezone(operator)
        for promise in promises:
            scheduled = _parse_promise_datetime(
                promise.scheduled_for_iso,
                local_tz=operator_tz,
            )
            if scheduled is None:
                _LOGGER.info(
                    "scheduled-promise skipped: unparseable iso=%r",
                    promise.scheduled_for_iso,
                )
                continue
            if scheduled <= now:
                _LOGGER.info(
                    "scheduled-promise skipped: past-dated %s (now=%s)",
                    scheduled.isoformat(), now.isoformat(),
                )
                continue
            row = _PendingFollowUp.new_promise(
                character_id=character_id,
                conversation_id=conversation_id,
                promise_intent=promise.intent,
                scheduled_for=scheduled,
                source_message_content=promise.source_text,
                source_content_mode=content_mode,
                now=now,
            )
            try:
                await repo.add(row)
                _LOGGER.info(
                    "scheduled-promise queued character=%s intent=%r "
                    "scheduled_for=%s",
                    character_id, promise.intent[:60],
                    scheduled.isoformat(),
                )
            except Exception:
                _LOGGER.exception(
                    "scheduled-promise persist failed character=%s",
                    character_id,
                )

    def _maybe_auto_consolidate(self, character_id: str) -> None:
        if self._auto_consolidation_trigger is None:
            return
        self._schedule_background(self._run_auto_consolidation(character_id))

    def _maybe_schedule_tts_pregeneration(
        self,
        *,
        character_id: str,
        assistant_text: str,
        user_id: str | None = None,
        content_mode: MessageContentMode | str = MessageContentMode.NORMAL,
    ) -> None:
        if self._tts_pregenerator is None or not assistant_text.strip():
            return
        self._schedule_background(
            self._tts_pregenerator.pregenerate_if_enabled(
                character_id=character_id,
                text=assistant_text,
                user_id=user_id,
                content_mode=content_mode,
            )
        )

    async def _run_auto_consolidation(self, character_id: str) -> None:
        if self._auto_consolidation_trigger is None:
            return
        try:
            await self._auto_consolidation_trigger.maybe_trigger(character_id)
        except Exception:
            _LOGGER.exception("Auto-consolidation trigger crashed")

    async def _apply_state_suggestion(
        self, character_id: str, suggestion: StateSuggestion,
    ) -> None:
        current = await self._character_repository.get(character_id)
        if current is None:
            return
        intent_kwargs: dict = {}
        if suggestion.current_intent is not None:
            intent_kwargs["current_intent"] = suggestion.current_intent
        refined = current.state.adjust(
            emotion=suggestion.emotion,
            affection_delta=suggestion.affection_delta,
            fatigue_delta=suggestion.fatigue_delta,
            trust_delta=suggestion.trust_delta,
            energy_delta=suggestion.energy_delta,
            **intent_kwargs,
        )
        await self._track(character_id, SOURCE_LLM_REFINEMENT, current.state, refined)
        await self._character_repository.save(current.with_state(refined))

    async def _apply_state_suggestion_compat(
        self, character_id: str, suggestion: StateSuggestion,
    ) -> None:
        """Apply only non-event state fields after event-sourced deltas.

        When an EmotionEvent was written, affection/fatigue/trust/energy
        and emotion label are derived at read time from the event stream.
        The flat columns remain a compatibility baseline; only
        ``current_intent`` still belongs in the column state.
        """
        if suggestion.current_intent is None:
            return
        current = await self._character_repository.get(character_id)
        if current is None:
            return
        refined = current.state.adjust(current_intent=suggestion.current_intent)
        await self._track(character_id, SOURCE_LLM_REFINEMENT, current.state, refined)
        await self._character_repository.save(current.with_state(refined))

    def _maybe_schedule_goal_review(
        self,
        *,
        character: Character,
        conversation: Conversation,
    ) -> None:
        if self._goal_service is None or self._goal_reviewer is None:
            return
        # Count completed user-assistant exchanges (each turn = 2 messages).
        turn_count = len(conversation.messages) // 2
        if turn_count == 0 or turn_count % self._goal_review_interval != 0:
            return
        coro = self._do_goal_review(
            character_id=character.id,
            conversation=conversation,
        )
        if self._extract_in_background:
            self._schedule_background(coro)
        else:
            # Synchronous path used by tests — still fire so behavior is
            # observable without requiring ``wait_for_pending``.
            self._schedule_background(coro)

    async def _do_goal_review(
        self,
        *,
        character_id: str,
        conversation: Conversation,
    ) -> None:
        if self._goal_service is None or self._goal_reviewer is None:
            return
        try:
            character = await self._character_repository.get(character_id)
            if character is None:
                return
            active = await self._goal_service.list_active_goals(character_id)
            recent = conversation.recent_messages(limit=_RECENT_MESSAGE_LIMIT)
            # new_goals.content / review notes render in PlayerGoalsPanel,
            # so pin them to the operator's content language (bug B2 class).
            operator = await self._load_operator(
                user_id=getattr(character, "user_id", None),
            )
            operator_language = (
                getattr(operator, "primary_language", "") or ""
            ).strip() or DEFAULT_PRIMARY_LANGUAGE
            result = await self._call_goal_reviewer(
                character=character,
                active_goals=active,
                recent_messages=recent,
                operator_primary_language=operator_language,
            )
        except Exception:
            _LOGGER.exception("Goal review crashed")
            return
        if not result.status_changes and not result.new_goals:
            return
        try:
            await self._goal_service.apply_review_result(
                character_id=character_id,
                result=result,
            )
        except Exception:
            _LOGGER.exception("Failed to apply goal review result")

    async def _call_goal_reviewer(
        self,
        *,
        character: Character,
        active_goals,  # noqa: ANN001 - list[CharacterGoal]
        recent_messages,  # noqa: ANN001 - list[Message]
        operator_primary_language: str,
    ):
        """Invoke the goal reviewer, passing the operator language only
        when the wired reviewer accepts it. Older / stub reviewers that
        predate the language kwarg keep working (they fall back to their
        own default) instead of raising a TypeError."""
        reviewer = self._goal_reviewer
        assert reviewer is not None
        if _accepts_keyword(reviewer.review, "operator_primary_language"):
            return await reviewer.review(
                character=character,
                active_goals=active_goals,
                recent_messages=recent_messages,
                operator_primary_language=operator_primary_language,
            )
        return await reviewer.review(
            character=character,
            active_goals=active_goals,
            recent_messages=recent_messages,
        )

    def _read_self_repetition_hint(self, conversation_id: str) -> str | None:
        """Return the cached anti-repetition hint for this conversation.

        Returns ``None`` when no extraction has run yet or the cache
        was cleared (e.g. process restart). The prompt builder treats
        ``None`` and ``""`` identically — the rail is just skipped.
        """
        entry = self._self_repetition_cache.get(conversation_id)
        if entry is None:
            return None
        _, hint = entry
        return hint or None

    def _maybe_schedule_repetition_check(
        self,
        *,
        character: Character,
        conversation: Conversation,
    ) -> None:
        """Fire-and-forget self-repetition extractor every N turns.

        Counts completed user-assistant exchanges and triggers on a
        cadence (default 5). No-op when the extractor isn't wired or
        the cadence hasn't hit. Same scheduling shape as
        ``_maybe_schedule_goal_review`` so behaviour stays predictable
        across aux LLM paths.
        """
        if self._self_repetition_extractor is None:
            return
        turn_count = len(conversation.messages) // 2
        if turn_count == 0 or turn_count % self._self_repetition_interval != 0:
            return
        cached = self._self_repetition_cache.get(conversation.id)
        # Same turn_index already extracted → skip. Guards against the
        # tool-cycle + finalize path double-firing for the same turn.
        if cached is not None and cached[0] == turn_count:
            return
        coro = self._do_repetition_check(
            character_id=character.id,
            conversation_id=conversation.id,
            turn_index=turn_count,
        )
        self._schedule_background(coro)

    async def _do_repetition_check(
        self,
        *,
        character_id: str,
        conversation_id: str,
        turn_index: int,
    ) -> None:
        """Actual extractor invocation. Loads fresh conversation +
        character (the in-memory ones passed by the chat path may be
        stale by the time this background task runs), runs the LLM,
        and writes the result into the cache for the next turn to
        consume. Swallow every exception — a flaky extractor must not
        break the chat path.
        """
        if self._self_repetition_extractor is None:
            return
        try:
            character = await self._character_repository.get(character_id)
            if character is None:
                return
            conversation = await self._conversation_repository.get(
                conversation_id,
            )
            if conversation is None:
                return
            assistant_msgs = [
                m for m in conversation.messages
                if m.role is MessageRole.ASSISTANT
                and m.kind is MessageKind.CHAT
                and m.content.strip()
            ]
            assistant_msgs = sanitize_messages_for_tolerance(
                assistant_msgs,
                content_tolerance=CONTENT_TOLERANCE_FRONTIER,
            )
            if not assistant_msgs:
                return
            window = assistant_msgs[-_SELF_REPETITION_WINDOW:]
            hint = await self._self_repetition_extractor.extract(
                character=character,
                recent_assistant_messages=window,
            )
        except Exception:
            _LOGGER.exception("self-repetition check crashed")
            return
        # Store even an empty hint so we don't keep re-extracting on
        # every turn within the same window when the LLM has already
        # said "nothing to flag" — the turn_index gate handles dedup.
        self._self_repetition_cache[conversation_id] = (turn_index, hint or "")

    async def _track(
        self,
        character_id: str,
        source: str,
        before: CharacterState,
        after: CharacterState,
        trigger: str | None = None,
    ) -> None:
        if self._state_tracker is not None:
            await self._state_tracker.record(
                character_id=character_id,
                source=source,
                before=before,
                after=after,
                trigger=trigger,
            )

    def _schedule_background(self, coro: Awaitable[None]) -> None:
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    async def wait_for_pending(self) -> None:
        """Await any fire-and-forget post-turn work. Intended for tests."""
        if not self._pending_tasks:
            return
        await asyncio.gather(*self._pending_tasks, return_exceptions=True)


def _tool_to_message_attachment(att: ToolAttachment) -> MessageAttachment:
    return MessageAttachment(
        kind=att.kind,
        url=att.url,
        mime_type=att.mime_type,
        caption=att.caption,
    )


def _classify_assistant_kind(
    text: str, attachments: tuple[MessageAttachment, ...] | list[MessageAttachment],
) -> MessageKind:
    """Assistant turn with empty text but a tool artifact (e.g. ``/pic``) is
    tagged ``TOOL_ONLY`` so schedule / arc / proactive context builders can
    filter it out — a bare image URL isn't useful dialogue."""
    if not text.strip() and len(attachments) > 0:
        return MessageKind.TOOL_ONLY
    return MessageKind.CHAT


_FORCED_IMAGE_TOOL_NAME = "generate_image"
_FORCED_IMAGE_TRIGGER_RE = re.compile(r"(?<!\S)/pic(?!\S)", re.IGNORECASE)

_RECENT_DIALOGUE_TURN_LIMIT = 4
_RECENT_DIALOGUE_CHAR_CAP = 800


_MAX_INLINE_IMAGE_BYTES = 10 * 1024 * 1024


def _parse_promise_datetime(
    raw: str | None,
    *,
    local_tz=timezone.utc,  # noqa: ANN001
) -> datetime | None:
    """Parse a ``MessagePromise.scheduled_for_iso`` string.

    Returns a timezone-aware UTC ``datetime`` or ``None`` for
    unparseable / out-of-range input. Naive datetimes are interpreted
    as the current user's timezone, then stored as UTC.
    """
    if not raw or not isinstance(raw, str):
        return None
    try:
        parsed = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(timezone.utc)


def _operator_timezone(operator: OperatorProfile | None):  # noqa: ANN201
    if operator is None:
        return timezone.utc
    try:
        return timezone_for_id(getattr(operator, "timezone_id", None))
    except ValueError:
        return timezone.utc


_BUSY_FAMILIARITY_LABELS: dict[str, str] = {
    "stranger": "互動還很少",
    "acquaintance": "互動漸多",
    "familiar": "互動頻繁",
    "close": "互動很密切",
}


def _render_busy_interaction_lines(strength: object | None) -> list[str]:
    if strength is None:
        return []
    total = int(getattr(strength, "total_user_messages", 0) or 0)
    if total <= 0 or getattr(strength, "first_message_at", None) is None:
        return [
            "- 與使用者互動熱度：互動量還很少；"
            "忙碌只能當背景，不要因互動紀錄少就假定關係很遠。",
        ]
    band = getattr(getattr(strength, "familiarity_band", None), "value", "")
    label = _BUSY_FAMILIARITY_LABELS.get(str(band), "互動漸多")
    if band == "close":
        note = "互動量已很高，偶爾延後比較不會破壞回覆節奏。"
    elif band == "familiar":
        note = "已有較多日常互動，但仍應讓對方感到被接住。"
    else:
        note = "互動量尚低，不要把忙碌變成冷落。"
    return [f"- 與使用者互動熱度：{label}；{note}"]


def _to_vision_url(
    url: str, *, uploads_dir: Path | None, public_base_url: str,
) -> str | None:
    """Convert a chat attachment URL into something the LLM can ingest.

    Priority order:

    1. ``data:image/...;base64,...`` already inlined → pass through.
    2. URL points at our own ``/uploads/`` mount (relative OR absolute
       under ``public_base_url``) → return the absolute public URL.
       Container deployments inline those through Object Storage in
       ``_to_vision_url_with_storage`` before this helper is reached.
    3. External ``http(s)://`` URL we don't own (e.g. Telegram CDN) →
       pass through. Models that accept HTTP (Anthropic, OpenAI cloud)
       handle it; models that don't (LM Studio) will error — there's
       no way around that without downloading first.
    4. Otherwise → ``None`` so the caller downgrades to the text
       placeholder.

    The Object Storage data-URL path is ``_MAX_INLINE_IMAGE_BYTES``-
    capped (default 10 MB) before this helper is reached.
    """
    _ = uploads_dir
    if not url:
        return None
    # Already a data: URL (caller pre-encoded). Keep as-is.
    if url.startswith("data:"):
        return url
    relative_url = url
    if (
        public_base_url
        and url.startswith(public_base_url)
        and url[len(public_base_url):].startswith("/uploads/")
    ):
        relative_url = url[len(public_base_url):]
    if relative_url.startswith("/uploads/"):
        if public_base_url:
            return f"{public_base_url}{relative_url}"
        return None
    # External URL (CDN, third party). Nothing we can do but
    # pass-through; the model adapter has to deal with it.
    if url.startswith(("http://", "https://")):
        return url
    return None


async def _to_vision_url_with_storage(
    url: str,
    *,
    uploads_dir: Path | None,
    public_base_url: str,
    object_storage: ObjectStoragePort | None,
) -> str | None:
    if object_storage is not None and url and not url.startswith("data:"):
        object_key = object_storage.object_key_from_url(url)
        if object_key is not None:
            try:
                metadata = await object_storage.stat(object_key=object_key)
                if metadata is not None and metadata.size_bytes > _MAX_INLINE_IMAGE_BYTES:
                    _LOGGER.warning(
                        "skipping inline image object %s (%d bytes > cap)",
                        object_key, metadata.size_bytes,
                    )
                    return None
                data = await object_storage.get_bytes(object_key=object_key)
                if len(data) > _MAX_INLINE_IMAGE_BYTES:
                    _LOGGER.warning(
                        "skipping inline image object %s (%d bytes > cap)",
                        object_key, len(data),
                    )
                    return None
                mime = (
                    metadata.content_type if metadata is not None
                    else mimetypes.guess_type(object_key)[0]
                ) or "image/png"
                b64 = base64.b64encode(data).decode("ascii")
                return f"data:{mime};base64,{b64}"
            except Exception:
                _LOGGER.exception(
                    "failed to read image object for inline encode key=%s",
                    object_key,
                )
                return None
    return _to_vision_url(
        url, uploads_dir=uploads_dir, public_base_url=public_base_url,
    )


def _build_vision_inventory(
    *,
    recent_messages: list[Message],
    current_user_urls: tuple[str, ...] | list[str],
    cap: int = _VISION_HISTORY_LIMIT,
) -> tuple[list[str], dict[int, list[int]]]:
    """Pick which image URLs to forward to the model this turn.

    Walks ``recent_messages`` in chronological order, then tacks on
    the current user message's attachments at the end. Keeps the
    newest ``cap`` images (FIFO) — older images get dropped when the
    budget is exceeded. Returns:

    - ``ordered_urls``: list of original ``/uploads/...`` (or
      external) URLs in the order they should appear to the model.
      ``image_urls[i]`` corresponds to the ``[圖 i+1]`` marker.
    - ``markers_by_turn``: maps ``recent_messages`` index (or
      ``len(recent_messages)`` for the current turn) → list of
      1-based marker numbers. The prompt builder uses this to inject
      ``[圖 N]`` placeholders into the right history line.

    ``cap=0`` returns an empty inventory — useful for disabling
    vision history without touching call sites.
    """
    if cap <= 0:
        return [], {}
    raw: list[tuple[int, str]] = []
    for idx, message in enumerate(recent_messages):
        for att in message.attachments:
            if att.kind == "image" and att.url:
                raw.append((idx, att.url))
    current_idx = len(recent_messages)
    for url in current_user_urls or ():
        if url:
            raw.append((current_idx, url))
    if not raw:
        return [], {}
    if len(raw) > cap:
        # Drop oldest until under budget — chronological order means
        # ``raw[0]`` is the oldest. Last items (current turn) win.
        raw = raw[-cap:]
    ordered_urls = [u for _, u in raw]
    markers_by_turn: dict[int, list[int]] = {}
    for marker_index, (turn_idx, _url) in enumerate(raw, start=1):
        markers_by_turn.setdefault(turn_idx, []).append(marker_index)
    return ordered_urls, markers_by_turn


def _image_recognition_prompt(image_count: int) -> str:
    # The output is conversation material for a roleplay model, not an
    # audit report: piles of "無法辨識／模糊" declarations leak into the
    # reply as "your message is hard to read" (turn record 9b094fad), so
    # illegible text is skipped silently instead of announced.
    markers = ", ".join(f"[圖 {i}]" for i in range(1, image_count + 1))
    return (
        "你會把對話中的圖片附件轉成可供純文字聊天模型使用的詳細文字脈絡。\n"
        f"共有 {image_count} 張圖片，請依照輸入順序使用 {markers} 標記。\n"
        "請用繁體中文輸出，逐張描述可見內容：主體、人物或角色、姿勢表情、"
        "服裝外觀、場景、物品、顏色、光線、構圖，以及可能影響"
        "對話理解的細節。\n"
        "畫面中的文字只寫出你能確實辨識的部分；看不清、太小或被裁切的文字"
        "直接略過，不要輸出「無法辨識」「模糊不清」這類宣告，也不要推測其內容。\n"
        "只描述畫面能支持的事實；不要推測身份、隱私資訊、敏感屬性或畫面外事件。\n"
        "輸出可以是條列，但不要加入對使用者的回覆、建議或角色扮演語氣。"
    )


def _clean_image_recognition_context(text: str | None) -> str:
    cleaned = "\n".join(
        line.rstrip()
        for line in str(text or "").strip().splitlines()
        if line.strip()
    )
    if len(cleaned) <= _IMAGE_RECOGNITION_CONTEXT_MAX_CHARS:
        return cleaned
    suffix = "\n（圖片識別摘要已截斷）"
    limit = max(0, _IMAGE_RECOGNITION_CONTEXT_MAX_CHARS - len(suffix))
    return cleaned[:limit].rstrip() + suffix


async def _prepare_vision_prompt(
    *,
    model,
    prompt: str,
    attachment_urls,
    public_base_url: str,
    uploads_dir: Path | None,
    object_storage: ObjectStoragePort | None = None,
    image_context: str = "",
) -> tuple[str, tuple[str, ...]]:
    """Split attachment URLs into (possibly-augmented prompt, urls).

    ``supports_vision=True`` → encode local files as ``data:`` URLs
    (most reliable) or use absolute URL fallback, and send alongside
    the text. Otherwise drop them. A non-empty ``image_context`` means
    the recognition summary is already rendered inside the prompt body
    (next to the ``[圖 N]`` legend — appending it here would land it
    after the instruction footer, where its analyst register reads as
    part of the user's message; turn record 9b094fad), so nothing is
    added. Without a summary, append the drop placeholder so the LLM
    knows the turn carried images it can't see."""
    urls = [u for u in (attachment_urls or ()) if u]
    if not urls:
        return prompt, ()
    supports_vision = bool(getattr(model, "supports_vision", False))
    if supports_vision:
        resolved: list[str] = []
        for u in urls:
            converted = await _to_vision_url_with_storage(
                u,
                uploads_dir=uploads_dir,
                public_base_url=public_base_url,
                object_storage=object_storage,
            )
            if converted:
                resolved.append(converted)
        if len(resolved) == len(urls):
            return prompt, tuple(resolved)
        _LOGGER.warning(
            "vision requested but %d/%d URLs couldn't be resolved to "
            "fetchable form; downgrading to text hint",
            len(urls) - len(resolved), len(urls),
        )
    if str(image_context or "").strip():
        return prompt, ()
    return prompt + _image_drop_placeholder(len(urls)), ()


def _format_recent_dialogue(
    recent_messages: list[Message], *, latest_user_message: str,
) -> str:
    """Render the last few turns as ``role: text`` lines for tools.

    Kept tight (4 turns, 800-char hard cap) — the rewriter only needs
    enough context to resolve pronouns, not a full transcript. The
    latest user message is appended if it isn't already the last entry,
    because ChatService sometimes passes ``recent_messages`` that stops
    at the prior turn.
    """
    if not recent_messages and not latest_user_message:
        return ""
    tail = recent_messages[-_RECENT_DIALOGUE_TURN_LIMIT:]
    lines: list[str] = []
    for msg in tail:
        role_label = "使用者" if msg.role == MessageRole.USER else "角色"
        content = (msg.content or "").strip()
        if not content:
            continue
        lines.append(f"{role_label}: {content}")
    latest = (latest_user_message or "").strip()
    if latest and (not lines or not lines[-1].endswith(latest)):
        lines.append(f"使用者: {latest}")
    text = "\n".join(lines)
    if len(text) > _RECENT_DIALOGUE_CHAR_CAP:
        text = text[-_RECENT_DIALOGUE_CHAR_CAP:]
    return text


def _digest_emotion_lines(events: list[EmotionEvent]) -> list[str]:
    lines: list[str] = []
    for event in events[:8]:
        parts = [
            event.created_at.isoformat(timespec="minutes"),
            event.cause_ref_kind,
            event.emotion_label,
            event.evidence_quote,
        ]
        text = " | ".join(part.strip() for part in parts if part and part.strip())
        if text:
            lines.append(_clip(text, 260))
    return lines


def _digest_reflection_lines(reflections: list) -> list[str]:
    lines: list[str] = []
    for reflection in reflections[:4]:
        narrative = getattr(reflection, "narrative", "")
        period = getattr(reflection, "period", "")
        text = " | ".join(part for part in (period, narrative) if part)
        if text.strip():
            lines.append(_clip(text, 320))
    return lines


def _digest_story_event_lines(events: list[StoryEvent]) -> list[str]:
    lines: list[str] = []
    for event in events[:6]:
        text = " | ".join(
            part
            for part in (
                event.date,
                event.emotional_tone or "",
                event.narrative,
            )
            if part and part.strip()
        )
        if text:
            lines.append(_clip(text, 320))
    return lines


def _digest_story_arc_lines(
    arc: "StoryArc | None",
    upcoming: list["StoryArcBeat"],
) -> list[str]:
    if arc is None:
        return []
    lines = [
        _clip(f"主題：{arc.title} | 前情：{arc.premise}", 360),
    ]
    for beat in upcoming[:4]:
        lines.append(
            _clip(
                f"{beat.scheduled_date.isoformat()} | {beat.title} | {beat.summary}",
                320,
            ),
        )
    return lines


def _digest_feed_lines(posts: tuple[FeedPost, ...]) -> list[str]:
    lines: list[str] = []
    for post in posts[:6]:
        text = " | ".join(
            part
            for part in (
                post.created_at.isoformat(timespec="minutes"),
                post.content_text or "",
            )
            if part and part.strip()
        )
        if text:
            lines.append(_clip(text, 260))
    return lines


def _material_digest_summary(
    digest: PromptMaterialDigest | None,
    *,
    enabled: bool,
) -> dict[str, object] | None:
    if not enabled and digest is None:
        return None
    metadata = dict(digest.digest_metadata) if digest is not None else {}
    return {
        "enabled": enabled,
        "applied": digest is not None,
        "bullet_count": len(digest.bullets) if digest is not None else 0,
        "provider_id": metadata.get("provider_id", ""),
        "model_id": metadata.get("model_id", ""),
        "latency_ms": metadata.get("latency_ms"),
        "prompt_tokens": metadata.get("prompt_tokens"),
        "completion_tokens": metadata.get("completion_tokens"),
        "error": metadata.get("error"),
    }


def _novelty_known_material_lines(
    *,
    material_digest: PromptMaterialDigest | None,
    emotion_events: list[EmotionEvent],
    self_reflections: list,
    story_events: list[StoryEvent],
    story_arc: "StoryArc | None",
    upcoming_arc_beats: list["StoryArcBeat"],
    recent_feed_posts: tuple[FeedPost, ...],
) -> list[str]:
    if material_digest is not None:
        return list(material_digest.bullets)
    return [
        *_digest_emotion_lines(emotion_events),
        *_digest_reflection_lines(self_reflections),
        *_digest_story_event_lines(story_events),
        *_digest_story_arc_lines(story_arc, upcoming_arc_beats),
        *_digest_feed_lines(recent_feed_posts),
    ]


def _recent_assistant_lines(messages: list[Message]) -> list[str]:
    lines: list[str] = []
    for message in reversed(messages):
        if message.role is MessageRole.ASSISTANT and message.content.strip():
            lines.append(_clip(message.content, 240))
        if len(lines) >= 4:
            break
    return lines


def _novelty_gate_summary(
    verdict: NoveltyVerdict | None,
    *,
    enabled: bool,
    retry_count: int,
) -> dict[str, object] | None:
    if not enabled and verdict is None:
        return None
    metadata = dict(verdict.gate_metadata) if verdict is not None else {}
    return {
        "enabled": enabled,
        "evaluated": verdict is not None,
        "passes": True if verdict is None else verdict.passes,
        "lacks_novelty": False if verdict is None else verdict.lacks_novelty,
        "imagery_relapse": False if verdict is None else verdict.imagery_relapse,
        "register_mismatch": False if verdict is None else verdict.register_mismatch,
        "over_warm": False if verdict is None else verdict.over_warm,
        "formulaic": False if verdict is None else verdict.formulaic,
        "feedback": "" if verdict is None else verdict.feedback,
        "retry_count": retry_count,
        "provider_id": metadata.get("provider_id", ""),
        "model_id": metadata.get("model_id", ""),
        "latency_ms": metadata.get("latency_ms"),
        "prompt_tokens": metadata.get("prompt_tokens"),
        "completion_tokens": metadata.get("completion_tokens"),
        "error": metadata.get("error"),
    }


def _reply_quality_risk_score(
    *,
    register_profile: RegisterProfile | None,
    diversity_evidence: ReplyDiversityEvidence | None,
    similarity_threshold: float,
) -> float:
    scores: list[float] = []
    if register_profile is not None:
        if register_profile.vulnerable_disclosure:
            scores.append(1.0)
        scores.extend((
            register_profile.emotional_intensity * register_profile.confidence,
            register_profile.seriousness * register_profile.confidence,
            register_profile.help_seeking * register_profile.confidence,
        ))
    if diversity_evidence is not None:
        if (
            diversity_evidence.max_self_similarity is not None
            and diversity_evidence.max_self_similarity >= similarity_threshold
        ):
            scores.append(diversity_evidence.max_self_similarity)
        if diversity_evidence.has_frequency_evidence:
            scores.append(0.7)
    return max(scores, default=0.0)


def _register_profile_summary(
    profile: RegisterProfile | None,
    *,
    enabled: bool,
) -> dict[str, object] | None:
    if not enabled and profile is None:
        return None
    metadata = dict(profile.metadata) if profile is not None else {}
    return {
        "enabled": enabled,
        "applied": profile is not None,
        "axes": dict(profile.axes) if profile is not None else {},
        "confidence": 0.0 if profile is None else profile.confidence,
        "vulnerable_disclosure": (
            False if profile is None else profile.vulnerable_disclosure
        ),
        "note": "" if profile is None else profile.note,
        "provider_id": metadata.get("provider_id", ""),
        "model_id": metadata.get("model_id", ""),
        "latency_ms": metadata.get("latency_ms"),
        "prompt_tokens": metadata.get("prompt_tokens"),
        "completion_tokens": metadata.get("completion_tokens"),
        "error": metadata.get("error"),
    }


def _diversity_evidence_summary(
    evidence: ReplyDiversityEvidence | None,
) -> dict[str, object] | None:
    if evidence is None:
        return None
    return {
        "assistant_line_count": evidence.assistant_line_count,
        "max_self_similarity": evidence.max_self_similarity,
        "mean_self_similarity": evidence.mean_self_similarity,
        "has_frequency_evidence": evidence.has_frequency_evidence,
        "metadata": dict(evidence.metadata),
    }


def _clip(text: str, limit: int) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def _resolve_image_trigger(
    *, character: Character, user_message: str,
) -> tuple[bool, str]:
    """Return ``(forced, cleaned_message)``.

    ``forced`` is ``True`` when the user explicitly includes the system
    image command (``/pic`` as a standalone token) and ``generate_image``
    is in ``allowed_tools``.

    ``cleaned_message`` is the user's text with the matched portion
    stripped out (and surrounding whitespace collapsed). Downstream
    uses it for persistence, prompt context, and the fallback tool
    call's ``positive`` so the command marker doesn't leak into dialogue
    history — leaving it in would mean next turn's LLM sees ``使用者:
    我想看你 /pic`` as context, which reads like a bug.

    Never returns an empty ``cleaned_message`` — if stripping would
    leave nothing, we keep the original so the conversation row still
    shows what the user typed. ``forced=False`` → cleaned_message is
    always the original input unchanged.
    """
    if _FORCED_IMAGE_TOOL_NAME not in character.allowed_tools:
        return False, user_message
    text = user_message or ""
    if not text.strip():
        return False, user_message
    match = _FORCED_IMAGE_TRIGGER_RE.search(text)
    if match is None:
        return False, user_message
    stripped = (text[: match.start()] + text[match.end():]).strip()
    # Collapse the whitespace gap the removal left behind so the
    # cleaned message doesn't have doubled spaces.
    stripped = re.sub(r"[ \t]+", " ", stripped).strip()
    cleaned = stripped if stripped else user_message
    return True, cleaned


def _resolve_chat_provider_and_model(
    *, character: Character, payload: SendChatMessageRequest,
) -> tuple[str, str | None]:
    """Pick the (provider_id, model_id) for the main chat reply.

    Per-character ``feature_models[FEATURE_CHAT]`` wins when set —
    operators can pin character A to Anthropic Sonnet while character B
    keeps using whatever the global dropdown is on. When the override
    pins only ``provider_id`` (``model_id`` blank) the override drives
    the provider but the model_id falls back to the request payload so
    the global dropdown's model still applies; same logic in reverse
    for an override with only ``model_id``. With no override at all we
    pass through the payload values unchanged, preserving the
    pre-feature behaviour exactly.
    """
    override = character.feature_model_for(FEATURE_CHAT)
    if override is None:
        return payload.provider_id or "", payload.model_id
    provider_id = override.provider_id or payload.provider_id or ""
    model_id = override.model_id if override.model_id else payload.model_id
    return provider_id, model_id


def _should_force_image_tool(
    *, character: Character, user_message: str,
) -> bool:
    """Thin wrapper retained for tests that only care about the trigger
    bool. New code should prefer :func:`_resolve_image_trigger` to get
    the cleaned message alongside."""
    forced, _ = _resolve_image_trigger(
        character=character, user_message=user_message,
    )
    return forced


async def _single_chunk_stream(text: str) -> AsyncIterator[str]:
    """Yield the pre-computed reply as a single streaming chunk.

    Used when the character has tools: we can't stream the real tokens
    because the first-hop model output might be a JSON tool call that
    would leak through the wire before we decide to suppress it.
    """
    if text:
        yield text


class StreamFinalizer:
    """Holds context needed to persist state after streaming completes."""

    def __init__(
        self,
        *,
        service: ChatService,
        character: "Character",
        conversation: Conversation,
        user_message: Message,
        pending_state: "CharacterState",
        used_memories: list[MemoryItem],
        prior_messages: list[Message],
        pre_resolved_text: str | None = None,
        pre_resolved_attachments: list[MessageAttachment] | None = None,
        journal: TurnJournal | None = None,
        prebuilt_response: ChatReplyResponse | None = None,
        persona_enabled: bool = True,
        trace: ChatGenerationTrace | None = None,
        safe_summary_model: ChatModelPort | None = None,
        safe_summary_model_id: str | None = None,
        stream_capture: object | None = None,
        forced_tool: bool = False,
        persona_curiosity_plan: PersonaCuriosityPlan | None = None,
        material_digest: PromptMaterialDigest | None = None,
        register_profile: RegisterProfile | None = None,
        diversity_evidence: ReplyDiversityEvidence | None = None,
        novelty_verdict: NoveltyVerdict | None = None,
        novelty_retry_count: int = 0,
        presence_frame: PresenceFrame | None = None,
        content_mode: MessageContentMode = MessageContentMode.NORMAL,
    ) -> None:
        self._service = service
        self._character = character
        self._conversation = conversation
        self._user_message = user_message
        self._pending_state = pending_state
        self._used_memories = used_memories
        self._prior_messages = prior_messages
        # When the tool-use path resolves the assistant text before
        # streaming starts, we stash it here so ``finish`` can trust
        # these values and doesn't need the caller to thread them back
        # through the SSE layer.
        self._pre_resolved_text = pre_resolved_text
        self._pre_resolved_attachments = pre_resolved_attachments or []
        self._journal = journal
        self._persona_enabled = persona_enabled
        self._trace = trace or ChatGenerationTrace()
        self._safe_summary_model = safe_summary_model
        self._safe_summary_model_id = safe_summary_model_id
        self._stream_capture = stream_capture
        self._forced_tool = forced_tool
        self._persona_curiosity_plan = persona_curiosity_plan
        self._material_digest = material_digest
        self._register_profile = register_profile
        self._diversity_evidence = diversity_evidence
        self._novelty_verdict = novelty_verdict
        self._novelty_retry_count = novelty_retry_count
        self._presence_frame = presence_frame or PresenceFrame.web_stage()
        self._content_mode = content_mode
        # When set, ``finish`` short-circuits and returns this response
        # without running state-update / post-turn / goal-review side
        # effects. Used by the busy-defer path: the brief ack has
        # already been persisted and the actual full reply will be
        # produced later by ``PendingFollowUpDispatcher``, where the
        # full post-turn pipeline runs against the resolved reply.
        self._prebuilt_response = prebuilt_response

    @property
    def conversation_id(self) -> str:
        return self._conversation.id

    async def finish(self, assistant_text: str) -> ChatReplyResponse:
        if self._prebuilt_response is not None:
            return self._prebuilt_response
        if self._pre_resolved_text is not None:
            # Trust the tool-cycle output; ignore whatever the stream
            # buffer accumulated (``_single_chunk_stream`` yielded this
            # same text, so they should match anyway).
            assistant_text = self._pre_resolved_text
            attachments = tuple(self._pre_resolved_attachments)
        else:
            attachments = ()
        assistant_message = Message(
            role=MessageRole.ASSISTANT,
            content=assistant_text,
            attachments=attachments,
            kind=_classify_assistant_kind(assistant_text, attachments),
            content_mode=self._content_mode,
        )
        state_engine = self._service._state_engine
        now = self._service._resolve_now()
        final_state = state_engine.on_assistant_reply(self._pending_state, assistant_text)
        final_state = final_state.with_active_now(now)
        await self._service._track(
            self._character.id, SOURCE_HEURISTIC,
            self._character.state, final_state,
            trigger=self._user_message.content[:80],
        )
        updated_character = self._character.with_state(final_state)
        # ``self._conversation`` already has the user message appended +
        # persisted by ``send_message_stream`` (so a mid-stream drop
        # doesn't lose the user's turn). Only append the assistant reply
        # here — re-appending the user message would duplicate it.
        updated_conversation = self._conversation.append(assistant_message)

        await self._service._character_repository.save(updated_character)
        await self._service._conversation_repository.save(updated_conversation)
        self._service._schedule_nsfw_safe_summary_generation(
            character=self._character,
            conversation_id=updated_conversation.id,
            user_position=len(updated_conversation.messages) - 2,
            assistant_position=len(updated_conversation.messages) - 1,
            content_mode=self._content_mode,
            model=self._safe_summary_model,
            model_id=self._safe_summary_model_id,
        )
        await self._service._touch_memories(self._used_memories)
        self._service._maybe_schedule_tts_pregeneration(
            character_id=self._character.id,
            assistant_text=assistant_text,
            user_id=self._character.user_id,
            content_mode=self._content_mode,
        )
        # Post-turn extraction always runs — even on command-forced
        # ``/pic`` turns. The trigger marker was stripped from the
        # persisted user message before we got here, so the extractor
        # sees clean conversational text and decides on its own whether
        # there's anything worth remembering.
        turn_record_id = str(uuid4())
        post_turn_refs = await self._service._run_post_turn(
            character=self._character,
            conversation_id=updated_conversation.id,
            turn_record_id=turn_record_id,
            user_text=self._user_message.content,
            assistant_text=assistant_text,
            prior_messages=self._prior_messages,
            persona_enabled=self._persona_enabled,
            content_mode=self._content_mode.value,
        )
        self._service._maybe_schedule_goal_review(
            character=self._character,
            conversation=updated_conversation,
        )
        self._service._maybe_schedule_repetition_check(
            character=self._character,
            conversation=updated_conversation,
        )
        await self._service._persist_journal(self._journal)

        turn_started_at = (
            self._journal.turn_started_at if self._journal is not None else None
        )
        latency_ms: int | None = None
        if turn_started_at is not None:
            latency_ms = int((now - turn_started_at).total_seconds() * 1000)
        trace = self._trace
        stream_metadata = (
            getattr(self._stream_capture, "metadata", None)
            if self._stream_capture is not None else None
        )
        if stream_metadata is not None:
            trace = _trace_from_metadata(
                prompt=trace.prompt_assembled,
                metadata=stream_metadata,
                fallback_model_id=trace.model_id,
                prompt_pack_hash=trace.prompt_pack_hash,
            )
        await self._service._record_turn_safely(TurnRecordingDraft(
            id=turn_record_id,
            character_id=self._character.id,
            kind="chat",
            model_id=trace.model_id,
            conversation_id=updated_conversation.id,
            prompt_assembled=trace.prompt_assembled,
            prompt_pack_hash=trace.prompt_pack_hash,
            response_text=assistant_text,
            latency_ms=trace.latency_ms if trace.latency_ms is not None else latency_ms,
            prompt_tokens=trace.prompt_tokens,
            completion_tokens=trace.completion_tokens,
            error=trace.error,
            post_turn_refs={
                "source": "send_message_stream",
                "turn_index": len(updated_conversation.messages),
                "memories_used": len(self._used_memories),
                "forced_tool": self._forced_tool,
                "content_mode": self._content_mode.value,
                "persona_curiosity": persona_curiosity_plan_summary(
                    self._persona_curiosity_plan,
                    surface="chat",
                ),
                "material_digest": _material_digest_summary(
                    self._material_digest,
                    enabled=self._service._prompt_material_digest_enabled,
                ),
                "register_profile": _register_profile_summary(
                    self._register_profile,
                    enabled=self._service._register_profile_enabled,
                ),
                "diversity": _diversity_evidence_summary(
                    self._diversity_evidence,
                ),
                "novelty_gate": _novelty_gate_summary(
                    self._novelty_verdict,
                    enabled=self._service._novelty_gate_enabled,
                    retry_count=self._novelty_retry_count,
                ),
                "presence_frame": self._presence_frame.to_metadata(),
                **post_turn_refs,
            },
        ))
        await self._service._record_llm_usage_safely(
            character_id=self._character.id,
            operator_id=getattr(self._character, "user_id", DEFAULT_OPERATOR_ID),
            conversation_id=updated_conversation.id,
            turn_record_id=turn_record_id,
            trace=trace,
            provider_id=str(
                getattr(self._safe_summary_model, "provider_id", "")
                or trace.model_id
                or "",
            ),
            source_surface="chat_stream",
            upstream_request_id=str(
                getattr(self._safe_summary_model, "last_request_id", "") or "",
            ),
            forced_tool=self._forced_tool,
        )

        return ChatReplyResponse.build(
            conversation_id=updated_conversation.id,
            user_message=self._user_message,
            assistant_message=assistant_message,
            state=final_state,
            assistant_turn_record_id=turn_record_id,
        )


def _accepts_keyword(func: object, name: str) -> bool:
    try:
        params = signature(func).parameters
    except (TypeError, ValueError):
        return True
    if name in params:
        return True
    return any(param.kind == param.VAR_KEYWORD for param in params.values())


def _with_nsfw_memory_tags(
    memories: list[MemoryItem],
    content_mode: str,
) -> list[MemoryItem]:
    if content_mode != CONTENT_MODE_NSFW:
        return memories
    tagged: list[MemoryItem] = []
    for item in memories:
        tags = tuple(dict.fromkeys((*item.tags, MEMORY_TAG_NSFW_MODE)))
        tagged.append(replace(item, tags=tags))
    return tagged


def _content_tolerance_for_model(
    model: ChatModelPort,
    *,
    content_mode: MessageContentMode,
) -> str:
    return content_tolerance_for_llm_provider(
        getattr(model, "provider_id", ""),
        current_content_mode=content_mode,
    )


def _content_tolerance_for_content_mode(
    content_mode: MessageContentMode,
) -> str:
    if content_mode is MessageContentMode.NSFW:
        return CONTENT_TOLERANCE_COMMUNITY
    return CONTENT_TOLERANCE_FRONTIER


def _replace_messages_at_positions(
    conversation: Conversation,
    *,
    replacements: dict[int, Message],
) -> Conversation:
    if not replacements:
        return conversation
    messages = [
        replacements.get(index, message)
        for index, message in enumerate(conversation.messages)
    ]
    return replace(conversation, messages=messages)


def _estimate_intensity_from_deltas(suggestion: "StateSuggestion") -> float:
    """Map raw deltas (signed ints, typical range ~[-15, 15]) to a
    0-1 intensity score so the aggregator can rank top events.

    Heuristic: max absolute delta across the four numeric channels,
    saturating at 15 → 1.0. An LLM ``emotion`` string with no deltas
    counts as 0.3 — meaningful but low salience.
    """
    deltas = (
        abs(suggestion.affection_delta),
        abs(suggestion.fatigue_delta),
        abs(suggestion.trust_delta),
        abs(suggestion.energy_delta),
    )
    peak = max(deltas) if deltas else 0
    if peak == 0:
        return 0.3 if (suggestion.emotion or "").strip() else 0.0
    return min(1.0, peak / 15.0)
