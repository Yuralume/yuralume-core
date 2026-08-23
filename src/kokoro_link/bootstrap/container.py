import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from typing import TYPE_CHECKING, Awaitable, Callable, Mapping
from urllib.parse import unquote, urlparse

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
    from sqlalchemy.orm import sessionmaker

    from kokoro_link.application.services.realtime_event_dispatcher import (
        RealtimeEventDispatcher,
        RealtimeEventRehydrator,
    )
    from kokoro_link.contracts.realtime_events import RealtimeOutboxPort
    from kokoro_link.application.services.background_shadow_coordinator import (
        ShadowCoordinator,
    )
    from kokoro_link.application.services.background_shadow_worker import (
        ShadowDryRunWorker,
    )
    from kokoro_link.contracts.background_jobs import (
        BackgroundCoordinatorLeasePort,
        BackgroundJobQueuePort,
    )
    from kokoro_link.contracts.execution_mode import RuntimeOwnershipPort
    from kokoro_link.application.services.external_chat_roster_service import (
        ExternalChatRosterService,
    )
    from kokoro_link.application.services.external_chat_attachment_service import (
        ExternalChatAttachmentService,
    )
    from kokoro_link.application.services.external_chat_turn_service import (
        ExternalChatTurnService,
    )

_LOGGER = logging.getLogger(__name__)
from kokoro_link.contracts.due_jobs import DEFAULT_CAPABILITY_CAPS
from kokoro_link.application.services.album_service import AlbumService
from kokoro_link.application.services.account_runtime_profile import (
    AccountRuntimeProfileResolver,
)
from kokoro_link.application.services.auto_consolidation_trigger import (
    AutoConsolidationTrigger,
)
from kokoro_link.application.services.account_runtime_profile_cache import (
    CachedAccountRuntimeProfileResolver,
    DEFAULT_PROFILE_CACHE_TTL_SECONDS,
)
from kokoro_link.application.services.channel_binding_service import ChannelBindingService
from kokoro_link.application.services.character_activity_anchor import (
    CharacterActivityAnchor,
)
from kokoro_link.application.services.character_activity_stats import (
    CharacterActivityStatsService,
)
from kokoro_link.application.services.character_draft_service import CharacterDraftService
from kokoro_link.application.services.character_creation_intake_service import (
    CharacterCreationIntakeService,
)
from kokoro_link.infrastructure.character_personality_type.llm_analyzer import (
    LLMCharacterPersonalityTypeAnalyzer,
)
from kokoro_link.application.services.companion_draft_service import CompanionDraftService
from kokoro_link.application.services.character_image_service import (
    CharacterImageService,
)
from kokoro_link.application.services.character_lora_service import (
    CharacterLoraService,
)
from kokoro_link.application.services.character_life_context import (
    CharacterLifeContextBuilder,
)
from kokoro_link.application.services.character_encounter_service import (
    CharacterEncounterMemoryWriter,
    CharacterEncounterPlanner,
    CharacterEncounterRunner,
    CharacterEncounterService,
)
from kokoro_link.application.services.character_relationship_service import (
    CharacterRelationshipService,
)
from kokoro_link.application.services.character_social_knowledge_service import (
    CharacterSocialKnowledgeService,
)
from kokoro_link.application.services.active_llm_provider import (
    PreferenceBackedActiveLLMProvider,
)
from kokoro_link.application.services.cloud_active_llm_provider import (
    CloudActiveLLMProvider,
)
from kokoro_link.infrastructure.usage.llm_metering import MeteredActiveLLMProvider
from kokoro_link.application.services.cloud_active_media_provider import (
    CloudActiveImageProvider,
    CloudActiveVideoProvider,
)
from kokoro_link.application.services.cloud_identity_resolver import (
    CloudOperatorIdentityResolver,
)
from kokoro_link.application.services.feature_keys import (
    FEATURE_ACTIVITY_AFTERMATH,
    FEATURE_ADDRESS_PREFERENCE_OBSERVER,
    FEATURE_ARC_ADAPT,
    FEATURE_ARC_BEAT_RECHECK,
    FEATURE_ARC_COMPLETION_MEMORY,
    FEATURE_ARC_CONTINUATION_DRAFT,
    FEATURE_ARC_PLAN,
    FEATURE_ARC_SCENE_WRITE,
    FEATURE_ARC_SEASON_DECIDE,
    FEATURE_BUSY_FOLLOW_UP,
    FEATURE_BUSY_REPLY_DECIDE,
    FEATURE_CARD_TRANSLATE,
    FEATURE_ARC_TEMPLATE_TRANSLATE,
    FEATURE_SILLYTAVERN_NORMALIZE,
    FEATURE_MEMOIR_LOCALIZE,
    FEATURE_SCHEDULED_PROMISE,
    FEATURE_DIALOGUE_SUMMARY,
    FEATURE_FEED_COMMENT_REPLY,
    FEATURE_IDLE_DRIFT,
    FEATURE_FEED_COMPOSE,
    FEATURE_VIDEO_STORYBOARD,
    FEATURE_BRANCHING_DRAMA,
    FEATURE_BRANCHING_DRAMA_CRITIC,
    FEATURE_BRANCHING_DRAMA_SCENE,
    FEATURE_CHARACTER_DRAFT,
    FEATURE_CHAT_ASSIST,
    FEATURE_CHAT_REPETITION_CHECK,
    FEATURE_EXPERIMENT_ANALYSIS,
    FEATURE_FUSION_STORY,
    FEATURE_FUSION_STORY_CRITIC,
    FEATURE_GOAL_REVIEW,
    FEATURE_MEMORY_CONSOLIDATE,
    FEATURE_NOVELTY_GATE,
    FEATURE_PERSONA_CURIOSITY,
    FEATURE_POST_TURN,
    FEATURE_PROACTIVE_INTENTION,
    FEATURE_PROMPT_MATERIAL_DIGEST,
    FEATURE_PROMPT_REWRITE,
    FEATURE_REGISTER_PROFILE,
    FEATURE_RELATIONSHIP_COHERENCE,
    FEATURE_SCHEDULE_PLAN,
    FEATURE_SCHEDULE_WEATHER_DRIFT,
    FEATURE_STORY_EXPAND,
    FEATURE_STORY_SCENE_CHIPS,
    FEATURE_STORY_SCENE_CLOSE,
    FEATURE_STORY_SCENE_OPEN,
    FEATURE_TTS_TRANSLATE,
)
from kokoro_link.application.services.character_service import CharacterService
from kokoro_link.application.services.character_primary_image_initializer import (
    CharacterPrimaryImageInitializer,
)
from kokoro_link.application.services.character_runtime_initializer import (
    CharacterRuntimeInitializer,
)
from kokoro_link.application.services.character_card_export_service import (
    CharacterCardExportService,
)
from kokoro_link.application.services.character_backup_export_service import (
    CharacterBackupExportService,
)
from kokoro_link.application.services.character_backup_import_service import (
    CharacterBackupImportService,
)
from kokoro_link.contracts.character_backup_jobs import (
    CharacterBackupJobRepositoryPort,
)
from kokoro_link.application.services.character_card_import_service import (
    CharacterCardImportService,
)
from kokoro_link.application.services.character_card_pack_service import (
    CharacterCardPackService,
)
from kokoro_link.application.services.exclusive_official_card_install import (
    ExclusiveOfficialCardInstaller,
)
from kokoro_link.application.services.official_card_pack_source import (
    OfficialCardPackSource,
)
from kokoro_link.infrastructure.character_card.pack_catalog import (
    CharacterCardPackCatalog,
)
from kokoro_link.infrastructure.cloud.official_card_catalog_client import (
    CachedOfficialCardCatalog,
    OfficialCardCatalogClient,
)
from kokoro_link.infrastructure.cloud.official_card_exclusive_client import (
    build_exclusive_payload_client,
)
from kokoro_link.infrastructure.cloud.official_card_gated_catalog_client import (
    build_gated_catalog_client,
)
from kokoro_link.infrastructure.character_card.llm_translator import (
    LLMCharacterCardTranslator,
)
from kokoro_link.application.services.sillytavern_convert_service import (
    SillyTavernConvertService,
)
from kokoro_link.infrastructure.character_card.sillytavern_normalizer import (
    LLMSillyTavernNormalizer,
)
from kokoro_link.infrastructure.memoir.llm_localizer import LLMMemoirLocalizer
from kokoro_link.application.services.chat_assist_service import ChatAssistService
from kokoro_link.application.services.chat_service import ChatService
from kokoro_link.application.services.chat_turn_lease import ChatTurnLease
from kokoro_link.application.services.drain_state import DrainState
from kokoro_link.application.services.turn_undo_service import TurnUndoService
from kokoro_link.infrastructure.prompt.llm_material_digester import (
    LLMPromptMaterialDigester,
)
from kokoro_link.infrastructure.prompt.llm_novelty_gate import LLMNoveltyGate
from kokoro_link.infrastructure.prompt.null_material_digester import (
    NullPromptMaterialDigester,
)
from kokoro_link.infrastructure.prompt.null_novelty_gate import NullNoveltyGate
from kokoro_link.infrastructure.register.llm_register_profiler import (
    LLMRegisterProfiler,
)
from kokoro_link.infrastructure.register.null_register_profiler import (
    NullRegisterProfiler,
)
from kokoro_link.contracts.active_llm import ActiveLLMProviderPort
from kokoro_link.contracts.object_storage import (
    ObjectNotFoundError,
    ObjectStorageError,
    ObjectStoragePort,
)
from kokoro_link.application.services.goal_review_service import (
    DailyGoalReviewService,
)
from kokoro_link.application.services.goal_service import GoalService
from kokoro_link.application.services.memory_admin_service import (
    MemoryAdminService,
)
from kokoro_link.application.services.memory_consolidation_service import (
    MemoryConsolidationService,
)
from kokoro_link.application.services.messaging_account_service import (
    MessagingAccountService,
)
from kokoro_link.application.services.nsfw_mode import NsfwModeService
from kokoro_link.application.services.output_language_policy import (
    OperatorOutputLanguageResolver,
)
from kokoro_link.application.services.discord_gateway_service import (
    DiscordGatewayService,
)
from kokoro_link.application.services.messaging_dispatcher import MessagingDispatcher
from kokoro_link.application.services.messaging_public_url import (
    MessagingPublicUrlResolver,
)
from kokoro_link.application.services.telegram_polling_service import (
    TelegramPollingService,
)
from kokoro_link.application.services.whatsapp_gateway_service import (
    WhatsAppGatewayService,
)
from kokoro_link.application.services.operator_persona_service import (
    OperatorPersonaService,
)
from kokoro_link.application.services.operator_persona_projection_service import (
    OperatorPersonaProjectionService,
)
from kokoro_link.application.services.persona_curiosity_service import (
    PersonaCuriosityService,
)
from kokoro_link.application.services.auth_service import AuthService
from kokoro_link.application.services.auth_strategy import (
    AuthStrategy,
    LocalAuthStrategy,
)
from kokoro_link.application.services.cloud_auth_service import (
    CloudFederatedAuthStrategy,
)
from kokoro_link.application.services.jwt_service import JWTService
from kokoro_link.application.services.operator_profile_service import (
    OperatorProfileService,
)
from kokoro_link.domain.entities.operator_profile import DEFAULT_OPERATOR_ID
from kokoro_link.application.services.provider_connection_service import (
    ProviderConnectionService,
)
from kokoro_link.infrastructure.security.password_hasher import (
    BcryptPasswordHasher,
    FakePasswordHasher,
    PasswordHasherPort,
)
from kokoro_link.infrastructure.cloud.user_service_client import (
    CloudUserServiceClient,
)
from kokoro_link.infrastructure.cloud.routing_profile_client import (
    CloudRoutingProfileClient,
)
from kokoro_link.application.services.cloud_routing_profile_cache import (
    CachedCloudRoutingProfileResolver,
)
from kokoro_link.contracts.cloud_routing_profile import CloudRoutingProfilePort
from kokoro_link.contracts.cloud_gateway import CloudGatewayIdentityResolverPort
from kokoro_link.infrastructure.cloud.tier_runtime_profile_client import (
    TierRuntimeProfileClient,
)
from kokoro_link.application.services.cloud_tier_profile_cache import (
    CachedTierRuntimeProfileResolver,
)
from kokoro_link.contracts.cloud_tier_runtime_profile import (
    TierRuntimeProfilePort,
)
from kokoro_link.infrastructure.cloud.credit_balance_client import (
    CreditBalanceClient,
)
from kokoro_link.application.services.cloud_credit_service import (
    CloudCreditService,
)
from kokoro_link.infrastructure.cloud.action_charge_client import (
    ActionChargeClient,
)
from kokoro_link.infrastructure.cloud.action_pricing_client import (
    ActionPricingClient,
    TierActionPricingClient,
)
from kokoro_link.application.services.cloud_action_billing_service import (
    CloudActionBillingService,
    NullActionBillingService,
)
from kokoro_link.application.services.cloud_pricing_service import (
    CloudPricingService,
)
from kokoro_link.application.services.cloud_tier_pricing_service import (
    CloudTierPricingService,
)
from kokoro_link.infrastructure.cloud.announcement_unread_client import (
    AnnouncementUnreadClient,
)
from kokoro_link.application.services.cloud_announcement_service import (
    CloudAnnouncementService,
)
from kokoro_link.application.services.quota_overage_service import (
    QuotaOverageService,
)
from kokoro_link.application.services.player_runtime_limits import (
    PlayerRuntimeLimitsService,
)
from kokoro_link.application.services.player_locale_service import (
    LOCALE_CLAIM_PREFIX,
    LOCALE_CLAIM_TTL_SECONDS,
    PlayerLocaleService,
)
from kokoro_link.contracts.geocoding import GeocodingPort
from kokoro_link.infrastructure.geo.open_meteo_geocoding_client import (
    OpenMeteoGeocodingClient,
)
from kokoro_link.infrastructure.llm.cloud_gateway_model import (
    CloudGatewayChatModel,
)
from kokoro_link.infrastructure.image.cloud_gateway_provider import (
    CloudGatewayImageProvider,
)
from kokoro_link.infrastructure.video.cloud_gateway_provider import (
    CloudGatewayVideoProvider,
)
from kokoro_link.application.services.persona_dream_service import (
    PersonaDreamService,
)
from kokoro_link.application.services.persona_extraction_service import (
    PersonaExtractionService,
)
from kokoro_link.contracts.persona_curiosity import PersonaCuriosityPlannerPort
from kokoro_link.application.services.feed_candidates import (
    FeedCandidateCollector,
)
from kokoro_link.application.services.feed_comment_reply_service import (
    FeedCommentReplyService,
)
from kokoro_link.application.services.feed_composer_service import (
    FeedComposerService,
)
from kokoro_link.application.services.feed_video_job_service import (
    FeedVideoJobService,
)
from kokoro_link.application.services.model_resolver import ModelResolver
from kokoro_link.application.services.video_storyboard_service import (
    VideoStoryboardService,
)
from kokoro_link.application.services.character_ttl_reaper import CharacterTtlReaper
from kokoro_link.application.services.tts_pregeneration_service import (
    TTSPregenerationService,
)
from kokoro_link.application.services.tts_service import TTSService
from kokoro_link.application.services.visual_generation_style import (
    VisualGenerationStyleService,
)
from kokoro_link.application.services.feed_event_bus import FeedEventBus
from kokoro_link.application.services.feed_comment_service import (
    FeedCommentService,
)
from kokoro_link.application.services.feed_reaction_memorializer import (
    FeedReactionMemorializer,
)
from kokoro_link.application.services.feed_reaction_service import (
    FeedReactionService,
)
from kokoro_link.application.services.proactive_dispatcher import ProactiveDispatcher
from kokoro_link.application.services.proactive_event_bus import ProactiveEventBus
from kokoro_link.application.services.proactive_scheduler import ProactiveScheduler
from kokoro_link.application.services.character_tick_executor import (
    CharacterTickExecutor,
)
from kokoro_link.application.services.social_tick_executor import (
    SocialTickExecutor,
)
from kokoro_link.infrastructure.observability.scheduler_metrics import (
    SchedulerMetrics,
)
from kokoro_link.application.services.event_curator_service import (
    EventCuratorService,
)
from kokoro_link.application.services.event_seed_dispenser import (
    EventSeedDispenser,
)
from kokoro_link.application.services.rss_ingestion_service import (
    RssIngestionService,
)
from kokoro_link.application.services.rss_source_sync_service import (
    RssSourceSyncService,
)
from kokoro_link.application.services.world_event_scheduler import (
    WorldEventScheduler,
)
from kokoro_link.contracts.character_event_mention import (
    CharacterEventMentionRepositoryPort,
)
from kokoro_link.contracts.character_event_inbox import (
    CharacterEventInboxRepositoryPort,
)
from kokoro_link.contracts.rss_feed_fetcher import RssFeedFetcherPort
from kokoro_link.contracts.rss_source import RssSourceRepositoryPort
from kokoro_link.contracts.world_event import WorldEventRepositoryPort
from kokoro_link.application.services.arc_template_intake_service import (
    ArcTemplateIntakeService,
)
from kokoro_link.application.services.arc_series_service import ArcSeriesService
from kokoro_link.application.services.beat_due_checker import BeatDueChecker
from kokoro_link.application.services.rest_recovery_refresher import (
    RestRecoveryRefresher,
)
from kokoro_link.application.services.branching_drama_critic import (
    BranchingDramaCritic,
)
from kokoro_link.application.services.branching_drama_polisher import (
    BranchingDramaPolisher,
)
from kokoro_link.application.services.branching_drama_director import (
    BranchingDramaDirector,
)
from kokoro_link.application.services.branching_drama_planner import (
    BranchingDramaPlanner,
)
from kokoro_link.application.services.branching_drama_service import (
    BranchingDramaService,
)
from kokoro_link.application.services.fusion_character_brief import (
    FusionCharacterBriefBuilder,
)
from kokoro_link.application.services.fusion_story_critic import (
    FusionStoryCritic,
)
from kokoro_link.application.services.fusion_story_planner import (
    FusionStoryPlanner,
)
from kokoro_link.application.services.fusion_story_polisher import (
    FusionStoryPolisher,
)
from kokoro_link.application.services.fusion_material_stats import (
    FusionMaterialStatsService,
)
from kokoro_link.application.services.fusion_story_service import (
    FusionStoryService,
)
from kokoro_link.application.services.drama_to_arc_draft_service import (
    DramaToArcDraftService,
)
from kokoro_link.application.services.fusion_to_arc_service import (
    FusionToArcDraftService,
)
from kokoro_link.application.services.studio_job_recovery import (
    StudioJobRecoveryService,
)
from kokoro_link.application.services.arc_series_continuation_draft_service import (
    ArcSeriesContinuationDraftService,
)
from kokoro_link.application.services.fusion_story_writer import (
    FusionStoryWriter,
)
from kokoro_link.application.services.story_arc_service import StoryArcService
from kokoro_link.application.services.story_beat_scene_service import (
    StoryBeatSceneService,
)
from kokoro_link.application.services.story_scene_material import (
    ForcedSeasonSceneMaterialProvider,
    PendingBeatSceneMaterialProvider,
)
from kokoro_link.application.services.story_scene_quota import (
    StorySceneQuotaGuard,
)
from kokoro_link.application.services.story_scene_service import (
    StorySceneService,
)
from kokoro_link.application.services.story_scene_side_story import (
    SideStorySceneMaterialProvider,
)
from kokoro_link.application.services.story_scene_timeout import (
    DEFAULT_STORY_SCENE_IDLE_TIMEOUT_SECONDS,
    StorySceneTimeoutCloser,
)
from kokoro_link.application.services.story_event_service import StoryEventService
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.application.services.schedule_memorializer import ScheduleMemorializer
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.application.services.schedule_weather_drift_service import (
    ScheduleWeatherDriftService,
)
from kokoro_link.application.services.state_tracker import StateChangeTracker
from kokoro_link.application.services.composer_tool_loop import ComposerToolLoop
from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
from kokoro_link.application.services.notification_service import NotificationService
from kokoro_link.bootstrap.settings import (
    AppSettings,
    TTSSettings,
    UserTimezoneSettings,
)
from kokoro_link.contracts.messaging import (
    ChannelAdapterPort,
    ChannelBindingRepositoryPort,
    MessagingAccountRepositoryPort,
)
from kokoro_link.contracts.album import AlbumRepositoryPort
from kokoro_link.contracts.feed import (
    FeedCommentRepositoryPort,
    FeedPostRepositoryPort,
    FeedReactionRepositoryPort,
)
from kokoro_link.contracts.pending_follow_up import (
    PendingFollowUpRepositoryPort,
)
from kokoro_link.contracts.proactive import ProactiveAttemptRepositoryPort
from kokoro_link.contracts.tool import (
    ToolInvocationRepositoryPort,
    ToolPort,
    ToolRegistryPort,
)
from kokoro_link.contracts.character_draft import (
    CharacterDraftGeneratorPort,
    CompanionDraftGeneratorPort,
)
from kokoro_link.contracts.clock import ClockPort
from kokoro_link.contracts.dialogue_summarizer import DialogueSummarizerPort
from kokoro_link.contracts.embedder import EmbedderPort
from kokoro_link.contracts.goal_repository import GoalRepositoryPort
from kokoro_link.contracts.goal_reviewer import GoalReviewerPort
from kokoro_link.contracts.llm import ChatModelRegistryPort
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.memory_consolidator import MemoryConsolidatorPort
from kokoro_link.contracts.nsfw_safe_summary import NsfwSafeSummaryPort
from kokoro_link.contracts.story import (
    StoryEventRepositoryPort,
    StorySeedRepositoryPort,
)
from kokoro_link.contracts.branching_drama import (
    BranchingDramaRepositoryPort,
)
from kokoro_link.contracts.fusion_story import FusionStoryRepositoryPort
from kokoro_link.contracts.async_video_job import (
    DEFAULT_VIDEO_JOB_TIMEOUT_SECONDS,
)
from kokoro_link.contracts.feed_video_jobs import (
    PendingFeedVideoRepositoryPort,
)
from kokoro_link.contracts.studio_jobs import StudioJobRepositoryPort
from kokoro_link.contracts.story_arc import (
    StoryArcPlannerPort,
    StoryArcRepositoryPort,
)
from kokoro_link.contracts.story_scene import (
    StorySceneSessionRepositoryPort,
)
from kokoro_link.contracts.arc_series import ArcSeriesRepositoryPort
from kokoro_link.contracts.post_turn import PostTurnProcessorPort
from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.contracts.operator_profile import OperatorProfileRepositoryPort
from kokoro_link.contracts.notifications import (
    NotificationPreferencesRepositoryPort,
    WebPushSenderPort,
    WebPushSubscriptionRepositoryPort,
)
from kokoro_link.contracts.repositories import (
    CharacterRepositoryPort,
    ConversationRepositoryPort,
    PreferencesRepositoryPort,
)
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.timezone import timezone_for_id
from kokoro_link.infrastructure.messaging.debounce import InboundDebouncer
from kokoro_link.infrastructure.messaging.discord.adapter import DiscordAdapter
from kokoro_link.infrastructure.messaging.discord.gateway_client import (
    DiscordGatewayClient,
)
from kokoro_link.infrastructure.messaging.discord.media_fetcher import (
    download_discord_attachment,
)
from kokoro_link.infrastructure.messaging.discord.parser import (
    parse_message_create as parse_discord_message_create,
)
from kokoro_link.infrastructure.messaging.line.adapter import LineAdapter
from kokoro_link.infrastructure.messaging.telegram.adapter import (
    LocalImageFetchResult,
    TelegramAdapter,
)
from kokoro_link.infrastructure.messaging.telegram.media_fetcher import (
    download_telegram_photo,
)
from kokoro_link.infrastructure.messaging.telegram.parser import (
    parse_update as parse_telegram_update,
)
from kokoro_link.infrastructure.messaging.whatsapp.adapter import WhatsAppAdapter
from kokoro_link.infrastructure.messaging.whatsapp.parser import (
    parse_whatsapp_event,
)
from kokoro_link.infrastructure.messaging.whatsapp.sidecar_client import (
    WhatsAppSidecarClient,
)
from kokoro_link.infrastructure.proactive.heuristic_gate import HeuristicProactiveGate
from kokoro_link.infrastructure.prompts import get_default_loader
from kokoro_link.infrastructure.proactive.llm_intention_judge import (
    LLMProactiveIntentionJudge,
    NullProactiveIntentionJudge,
)
from kokoro_link.infrastructure.proactive.llm_decider import LLMProactiveDecider
from kokoro_link.infrastructure.proactive.null_decider import NullProactiveDecider
from kokoro_link.infrastructure.notifications.pywebpush_sender import (
    NullWebPushSender,
    PyWebPushSender,
    WebPushVapidConfig,
)
from kokoro_link.infrastructure.repositories.in_memory_channel_bindings import (
    InMemoryChannelBindingRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_notifications import (
    InMemoryNotificationPreferencesRepository,
    InMemoryWebPushSubscriptionRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_messaging_accounts import (
    InMemoryMessagingAccountRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_proactive_attempts import (
    InMemoryProactiveAttemptRepository,
)
from kokoro_link.contracts.schedule_planner import SchedulePlannerPort
from kokoro_link.contracts.schedule_repository import ScheduleRepositoryPort
from kokoro_link.contracts.state_history import StateHistoryRepositoryPort
from kokoro_link.contracts.turn_journal import TurnJournalRepositoryPort
from kokoro_link.contracts.behavioral_pattern import (
    BehavioralPatternRepositoryPort,
)
from kokoro_link.contracts.deferred_intent import (
    DeferredIntentRepositoryPort,
)
from kokoro_link.contracts.disposition_drift import (
    DispositionDriftHistoryRepositoryPort,
)
from kokoro_link.contracts.observability import TurnRecordRepositoryPort
from kokoro_link.contracts.emotion import EmotionEventRepositoryPort
from kokoro_link.contracts.self_reflection import (
    SelfReflectionRepositoryPort,
)
from kokoro_link.contracts.memoir import MemoirPinRepositoryPort
from kokoro_link.contracts.runtime_settings import (
    RuntimeSettingsRepositoryPort,
)
from kokoro_link.contracts.provider_settings import (
    ProviderConnectionRepositoryPort,
)
from kokoro_link.application.services.quiet_hours_service import (
    QuietHoursService,
)
from kokoro_link.contracts.operator_address_preference import (
    OperatorAddressPreferenceRepositoryPort,
)
from kokoro_link.application.services.address_preference_observer_service import (
    AddressPreferenceObserverService,
)
from kokoro_link.application.services.relationship_names_service import (
    RelationshipNamesService,
)
from kokoro_link.contracts.player_persona_note import (
    PlayerPersonaNoteRepositoryPort,
)
from kokoro_link.application.services.player_persona_note_service import (
    PlayerPersonaNoteService,
)
from kokoro_link.contracts.address_change_log import (
    AddressChangeLogRepositoryPort,
)
from kokoro_link.contracts.experiment import (
    ExperimentAssignmentRepositoryPort,
    ExperimentRepositoryPort,
)
from kokoro_link.application.services.experiment_service import (
    ExperimentService,
)
from kokoro_link.application.services.experiment_overlay_service import (
    ExperimentOverlayService,
)
from kokoro_link.application.services.experiment_analysis_service import (
    ExperimentAnalysisService,
)
from kokoro_link.infrastructure.llm.priority_gate import (
    LLMSerialisationGate,
)
from kokoro_link.infrastructure.character_draft.llm_companion_generator import (
    LLMCompanionDraftGenerator,
)
from kokoro_link.infrastructure.character_draft.llm_generator import LLMCharacterDraftGenerator
from kokoro_link.infrastructure.character_draft.stub import (
    StubCharacterDraftGenerator,
    StubCompanionDraftGenerator,
)
from kokoro_link.infrastructure.dialogue.llm_safe_summary import LLMNsfwSafeSummarizer
from kokoro_link.infrastructure.dialogue.llm_summarizer import LLMDialogueSummarizer
from kokoro_link.infrastructure.dialogue.null_safe_summary import NullNsfwSafeSummarizer
from kokoro_link.infrastructure.dialogue.null_summarizer import NullDialogueSummarizer
from kokoro_link.infrastructure.embedder.cloud_gateway import CloudGatewayEmbedder
from kokoro_link.infrastructure.embedder.lm_studio import LMStudioEmbedder
from kokoro_link.infrastructure.embedder.null import NullEmbedder
from kokoro_link.infrastructure.embedder.runtime import RuntimeConfigurableEmbedder
from kokoro_link.infrastructure.goal.llm_reviewer import LLMGoalReviewer
from kokoro_link.infrastructure.self_repetition.llm_extractor import (
    LLMSelfRepetitionExtractor,
)
from kokoro_link.infrastructure.goal.null_reviewer import NullGoalReviewer
from kokoro_link.infrastructure.llm.fake import FakeChatModel
from kokoro_link.infrastructure.llm.registry import InMemoryChatModelRegistry
from kokoro_link.infrastructure.memory.in_memory import InMemoryMemoryRepository
from kokoro_link.infrastructure.repositories.in_memory_stories import (
    InMemoryStoryEventRepository,
    InMemoryStorySeedRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_arcs import (
    InMemoryStoryArcRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_story_scene_sessions import (
    InMemoryStorySceneSessionRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_fusion_stories import (
    InMemoryFusionStoryRepository,
)
from kokoro_link.infrastructure.story.llm_expander import (
    LLMStoryEventExpander,
    NullStoryEventExpander,
)
from kokoro_link.infrastructure.story.llm_beat_scene_writer import (
    LLMStoryBeatSceneWriter,
)
from kokoro_link.infrastructure.story.llm_scene_chips import (
    LLMStorySceneChipsWriter,
)
from kokoro_link.infrastructure.story.llm_scene_closer import (
    LLMStorySceneCloser,
)
from kokoro_link.infrastructure.story.llm_scene_opener import (
    LLMStorySceneOpener,
)
from kokoro_link.infrastructure.story.drama_to_arc_adapter import (
    LLMDramaToArcAdapter,
)
from kokoro_link.infrastructure.story.fusion_to_arc_adapter import (
    LLMFusionToArcAdapter,
)
from kokoro_link.infrastructure.story.arc_series_continuation_adapter import (
    LLMArcSeriesContinuationDraftAdapter,
)
from kokoro_link.infrastructure.story.yaml_arc_template_repository import (
    YAMLArcTemplatePackLoader,
)
from kokoro_link.infrastructure.persistence.sa_arc_template_repository import (
    SAArcTemplateRepository,
)
from kokoro_link.infrastructure.persistence.sa_arc_series_repository import (
    SAArcSeriesRepository,
)
from kokoro_link.application.services.arc_template_pack_sync_service import (
    ArcTemplatePackSyncService,
)
from kokoro_link.contracts.arc_template import ArcTemplateRepositoryPort
from kokoro_link.contracts.arc_template_translator import (
    ArcTemplateTranslatorPort,
)
from kokoro_link.infrastructure.story.llm_arc_template_translator import (
    LLMArcTemplateTranslator,
)
from kokoro_link.infrastructure.story.llm_arc_planner import (
    LLMStoryArcPlanner,
    NullStoryArcPlanner,
)
from kokoro_link.infrastructure.story.llm_season_decider import (
    LLMStoryArcSeasonDecider,
    NullStoryArcSeasonDecider,
)
from kokoro_link.infrastructure.story.llm_beat_rechecker import (
    LLMStoryBeatRechecker,
    NullStoryBeatRechecker,
)
from kokoro_link.infrastructure.story.llm_arc_completion_memory_writer import (
    LLMArcCompletionMemoryWriter,
)
from kokoro_link.infrastructure.memory.llm_consolidator import LLMMemoryConsolidator
from kokoro_link.infrastructure.memory.null_consolidator import NullMemoryConsolidator
from kokoro_link.infrastructure.post_turn.llm_processor import LLMPostTurnProcessor
from kokoro_link.infrastructure.post_turn.null_processor import NullPostTurnProcessor
from kokoro_link.infrastructure.social.llm_peer_knowledge_consolidator import (
    LLMPeerKnowledgeConsolidator,
)
from kokoro_link.infrastructure.prompt.default import (
    DefaultPromptContextBuilder,
    prompt_pack_hash_snapshot,
)
from kokoro_link.infrastructure.time import SystemClock
from kokoro_link.infrastructure.repositories.in_memory_characters import InMemoryCharacterRepository
from kokoro_link.infrastructure.repositories.in_memory_character_encounters import (
    InMemoryCharacterEncounterRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_character_encounter_intents import (
    InMemoryCharacterEncounterIntentRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_templates import (
    InMemoryArcTemplateRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_arc_series import (
    InMemoryArcSeriesRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_character_relationships import (
    InMemoryCharacterRelationshipRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_character_peer_profiles import (
    InMemoryCharacterPeerProfileRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_conversations import InMemoryConversationRepository
from kokoro_link.infrastructure.repositories.in_memory_goals import InMemoryGoalRepository
from kokoro_link.infrastructure.repositories.in_memory_schedules import InMemoryScheduleRepository
from kokoro_link.infrastructure.repositories.in_memory_state_history import InMemoryStateHistoryRepository
from kokoro_link.infrastructure.repositories.in_memory_turn_journals import (
    InMemoryTurnJournalRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_preferences import (
    InMemoryPreferencesRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_operator_profile import (
    InMemoryOperatorProfileRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_initial_relationship import (
    InMemoryCharacterOperatorRelationshipSeedRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_album import (
    InMemoryAlbumRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_comments import (
    InMemoryFeedCommentRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_posts import (
    InMemoryFeedPostRepository,
)
from kokoro_link.infrastructure.repositories.in_memory_feed_reactions import (
    InMemoryFeedReactionRepository,
)
from kokoro_link.infrastructure.feed.llm_composer import LLMFeedComposer
from kokoro_link.infrastructure.feed.llm_comment_reply import (
    LLMFeedCommentReplyComposer,
    NullFeedCommentReplyComposer,
)
from kokoro_link.infrastructure.feed.null_composer import NullFeedComposer
from kokoro_link.infrastructure.schedule.llm_aftermath import (
    LLMActivityAftermathJudge,
    NullActivityAftermathJudge,
)
from kokoro_link.infrastructure.schedule.llm_weather_drift import (
    LLMScheduleWeatherDriftJudge,
    NullScheduleWeatherDriftJudge,
)
from kokoro_link.infrastructure.state.llm_idle_drift import (
    LLMIdleDriftJudge,
    NullIdleDriftJudge,
)
from kokoro_link.infrastructure.busy.llm_decider import (
    LLMBusyReplyDecider,
)
from kokoro_link.infrastructure.busy.null_decider import (
    NullBusyReplyDecider,
)
from kokoro_link.infrastructure.busy.llm_follow_up_composer import (
    LLMPendingFollowUpComposer,
    NullPendingFollowUpComposer,
)
from kokoro_link.infrastructure.busy.llm_scheduled_promise_composer import (
    LLMScheduledPromiseComposer,
    NullScheduledPromiseComposer,
)
from kokoro_link.application.services.pending_follow_up_dispatcher import (
    PendingFollowUpDispatcher,
)
from kokoro_link.contracts.tts_catalog import TTSVoiceCatalogPort
from kokoro_link.infrastructure.tts.external_api import (
    ExternalTTSAdapter,
    OpenAITTSAdapter,
)
from kokoro_link.infrastructure.tts.cloud_gateway import CloudGatewayTTSAdapter
from kokoro_link.infrastructure.tts.llm_translator import (
    LLMTTSTranslator,
    NullTTSTranslator,
)
from kokoro_link.infrastructure.tts.null import NullTTSAdapter
from kokoro_link.infrastructure.repositories.in_memory_tool_invocations import (
    InMemoryToolInvocationRepository,
)
from kokoro_link.application.services.active_image_provider import (
    PreferenceBackedActiveImageProvider,
)
from kokoro_link.application.services.active_video_provider import (
    PreferenceBackedActiveVideoProvider,
)
from kokoro_link.bootstrap.image_profiles import load_image_profiles
from kokoro_link.bootstrap.video_profiles import load_video_profiles
from kokoro_link.contracts.active_image import ActiveImageProviderPort
from kokoro_link.contracts.scene_image import SceneImagePort
from kokoro_link.domain.entities.branching_drama import IMAGE_PREFETCH_DEPTH
from kokoro_link.contracts.active_video import ActiveVideoProviderPort
from kokoro_link.contracts.image_profile import (
    ExternalImageApiProfileConfig,
)
from kokoro_link.contracts.video_profile import (
    ExternalVideoApiProfileConfig,
)
from kokoro_link.infrastructure.image.active_provider_scene_image import (
    ActiveProviderSceneImageAdapter,
)
from kokoro_link.infrastructure.image.comfy_scene_image import (
    ComfySceneImageAdapter,
)
from kokoro_link.infrastructure.image.profile_registry import (
    ImageProfileRegistry,
)
from kokoro_link.infrastructure.video.profile_registry import (
    VideoProfileRegistry,
)
from kokoro_link.infrastructure.tools.comfyui.client import AsyncComfyUiClient
from kokoro_link.infrastructure.tools.comfyui.generator import (
    ComfyPortraitGenerator,
)
from kokoro_link.infrastructure.tools.comfyui.scene_generator import (
    ComfySceneGenerator,
)
from kokoro_link.infrastructure.tools.comfyui.prompt_rewriter import (
    LLMPromptRewriter,
)
from kokoro_link.infrastructure.tools.comfyui.tool import ComfyImageTool
from kokoro_link.infrastructure.tools.comfyui.workflow import (
    DEFAULT_WORKFLOW_FILE,
    WorkflowBuilder,
)
from kokoro_link.infrastructure.tools.registry import InMemoryToolRegistry
from kokoro_link.infrastructure.tools.webfetch import (
    HttpxReadabilityFetcher,
    WebFetchTool,
)
from kokoro_link.infrastructure.tools.websearch import (
    CloudGatewaySearchClient,
    TavilyClient,
    WebSearchTool,
)
from kokoro_link.bootstrap.site_settings_holder import (
    SiteSettingsHolder,
    SiteSettingsSnapshot,
)
from kokoro_link.bootstrap.site_settings_providers import (
    ReloadableCalendarProvider,
    ReloadableGeoLocationProvider,
    ReloadableWeatherProvider,
    build_calendar_provider,
    build_geo_location_provider,
    build_weather_provider,
)
from kokoro_link.contracts.calendar_context import CalendarContextPort
from kokoro_link.contracts.geo_location import GeoLocationPort
from kokoro_link.contracts.character_encounter import (
    CharacterEncounterRepositoryPort,
)
from kokoro_link.contracts.character_encounter_intent import (
    CharacterEncounterIntentRepositoryPort,
)
from kokoro_link.contracts.character_relationship import (
    CharacterRelationshipRepositoryPort,
)
from kokoro_link.contracts.character_peer_profile import (
    CharacterPeerProfileRepositoryPort,
)
from kokoro_link.contracts.weather_context import WeatherContextPort
from kokoro_link.infrastructure.schedule.llm_planner import LLMSchedulePlanner
from kokoro_link.infrastructure.schedule.null_planner import NullSchedulePlanner
from kokoro_link.infrastructure.schedule.stub_planner import StubSchedulePlanner
from kokoro_link.infrastructure.state.simple import SimpleStateEngine
from kokoro_link.infrastructure.storage.http import HttpObjectStorage
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
from kokoro_link.infrastructure.storage.variant_aware import (
    VariantAwareObjectStorage,
)
from kokoro_link.infrastructure.security.provider_secret_cipher import (
    ProviderSecretCipher,
)

_FAKE_PROVIDER_ID = "fake"


@dataclass(slots=True)
class ServiceContainer:
    character_service: CharacterService
    chat_service: ChatService
    goal_service: GoalService
    schedule_service: ScheduleService
    character_draft_service: CharacterDraftService
    companion_draft_service: CompanionDraftService
    character_image_service: CharacterImageService
    character_lora_service: CharacterLoraService
    character_relationship_service: CharacterRelationshipService
    character_encounter_service: CharacterEncounterService
    album_service: AlbumService
    tool_registry: ToolRegistryPort
    tool_orchestrator: ToolOrchestrator
    tool_invocation_repository: ToolInvocationRepositoryPort
    memory_admin_service: MemoryAdminService
    memory_consolidation_service: MemoryConsolidationService
    state_history_repository: StateHistoryRepositoryPort
    embedder: EmbedderPort
    provider_ids: list[str]
    model_registry: ChatModelRegistryPort
    preferences_repository: PreferencesRepositoryPort
    schedule_memorializer: ScheduleMemorializer | None = None
    schedule_weather_drift_service: ScheduleWeatherDriftService | None = None
    active_llm_provider: ActiveLLMProviderPort | None = None
    cloud_routing_profile_resolver: CloudRoutingProfilePort | None = None
    cloud_mode: bool = False
    """Hosted deployment. Read by ``runtime_sync`` to keep DB-backed BYOK
    provider rows out of registries the Gateway owns in hosted mode."""
    nsfw_mode_service: NsfwModeService | None = None
    visual_generation_style_service: VisualGenerationStyleService | None = None
    object_storage: ObjectStoragePort = field(
        default_factory=lambda: InMemoryObjectStorage(),
    )
    conversation_repository: "ConversationRepositoryPort | None" = None
    image_profile_registry: ImageProfileRegistry = field(
        default_factory=lambda: ImageProfileRegistry([]),
    )
    """Default = empty registry. Test harnesses that construct
    ``ServiceContainer`` directly don't need image generation wired —
    they can ignore the field, and any call to the image routes will
    cleanly return 'no profile configured' instead of crashing."""
    video_profile_registry: VideoProfileRegistry = field(
        default_factory=lambda: VideoProfileRegistry([]),
    )
    """Default = empty registry. Same rationale as
    :attr:`image_profile_registry` — tests skip video config entirely
    and the API surface degrades to 'no profile configured'."""
    operator_profile_repository: OperatorProfileRepositoryPort | None = None
    relationship_seed_repository: (
        CharacterOperatorRelationshipSeedRepositoryPort | None
    ) = None
    operator_profile_service: OperatorProfileService | None = None
    geo_location_provider: GeoLocationPort | None = None
    site_settings_holder: SiteSettingsHolder | None = None
    """Live weather/calendar/geoip/world_events settings (G0 hot reload).

    ``None`` only on hand-built test containers. The admin write path and the
    site-settings refresher both converge THIS holder; every consumer reads
    through it instead of a boot-time copy."""
    site_settings_reloader: "Callable[[], Awaitable[None]] | None" = None
    """Re-reads the hot groups into :attr:`site_settings_holder`.

    One callback shared by the local admin write path and the cross-process
    refresher, so "what a save applies here" and "what a NOTIFY applies there"
    can never drift apart."""
    auth_service: "AuthService | None" = None
    auth_strategy: "AuthStrategy | None" = None
    password_hasher: "PasswordHasherPort | None" = None
    jwt_service: "JWTService | None" = None
    story_seed_repository: StorySeedRepositoryPort | None = None
    story_event_repository: StoryEventRepositoryPort | None = None
    story_event_service: StoryEventService | None = None
    story_beat_scene_service: StoryBeatSceneService | None = None
    story_arc_repository: StoryArcRepositoryPort | None = None
    story_scene_service: StorySceneService | None = None
    story_scene_session_repository: StorySceneSessionRepositoryPort | None = None
    story_arc_service: StoryArcService | None = None
    arc_template_repository: ArcTemplateRepositoryPort | None = None
    arc_template_translator: ArcTemplateTranslatorPort | None = None
    arc_template_intake_service: "ArcTemplateIntakeService | None" = None
    character_creation_intake_service: CharacterCreationIntakeService | None = None
    arc_template_pack_sync_service: "ArcTemplatePackSyncService | None" = None
    arc_series_repository: ArcSeriesRepositoryPort | None = None
    arc_series_service: ArcSeriesService | None = None
    arc_series_continuation_draft_service: (
        "ArcSeriesContinuationDraftService | None"
    ) = None
    character_card_export_service: "CharacterCardExportService | None" = None
    character_card_import_service: "CharacterCardImportService | None" = None
    sillytavern_convert_service: "SillyTavernConvertService | None" = None
    character_card_pack_service: "CharacterCardPackService | None" = None
    character_primary_image_initializer: CharacterPrimaryImageInitializer | None = (
        None
    )
    character_runtime_initializer: "CharacterRuntimeInitializer | None" = None
    chat_assist_service: ChatAssistService | None = None
    turn_journal_repository: TurnJournalRepositoryPort | None = None
    turn_undo_service: TurnUndoService | None = None
    messaging_dispatcher: MessagingDispatcher | None = None
    telegram_polling_service: TelegramPollingService | None = None
    discord_gateway_service: DiscordGatewayService | None = None
    whatsapp_gateway_service: WhatsAppGatewayService | None = None
    messaging_account_service: MessagingAccountService | None = None
    channel_binding_service: ChannelBindingService | None = None
    web_push_subscription_repository: (
        WebPushSubscriptionRepositoryPort | None
    ) = None
    notification_preferences_repository: (
        NotificationPreferencesRepositoryPort | None
    ) = None
    web_push_sender: WebPushSenderPort | None = None
    notification_service: NotificationService | None = None
    proactive_attempt_repository: ProactiveAttemptRepositoryPort | None = None
    proactive_dispatcher: ProactiveDispatcher | None = None
    proactive_scheduler: ProactiveScheduler | None = None
    # P3-A tick executors (HOSTED_CORE_SCALING §13). The scheduler owns these and
    # delegates the per-character / global-social tick bodies to them; stored here
    # too so the distributed worker (P3-C) reuses the byte-identical executors.
    character_tick_executor: CharacterTickExecutor | None = None
    social_tick_executor: SocialTickExecutor | None = None
    # Phase 0 metrics registry (HOSTED_CORE_SCALING §12): fed by the scheduler
    # each tick, read by the internal metrics route. ``None`` on bare
    # ``ServiceContainer()`` test harnesses.
    scheduler_metrics: SchedulerMetrics | None = None
    # GD1-A rolling-deploy drain switch. One per container = one per process, which is the scope a drain
    # request addresses. Always present — including on hand-built test
    # containers — so the internal drain route and the metrics exporter never
    # have to answer "what if nobody wired it". Defaults to *not* draining, and
    # not draining is byte-for-byte the historical behaviour.
    drain_state: DrainState = field(default_factory=DrainState)
    # P2-B shadow runtime (HOSTED_CORE_SCALING §13 Phase 2). All ``None`` unless
    # YURALUME_BACKGROUND_SHADOW=postgres on a scheduler-owning role with a DB —
    # the self-host red line is that leaving the env unset changes nothing. The
    # queue + lease ports are exposed so the internal metrics route and the
    # admin diagnostics route can read live queue stats / lease info.
    background_shadow_coordinator: "ShadowCoordinator | None" = None
    background_shadow_worker: "ShadowDryRunWorker | None" = None
    background_job_queue: "BackgroundJobQueuePort | None" = None
    background_coordinator_lease: "BackgroundCoordinatorLeasePort | None" = None
    # Execution-ownership port (P3-B, §2.2 / §15). Wired ONLY on the hosted
    # opt-in (background_backend=='postgres' AND a DB). Self-host default leaves
    # it None so the embedded scheduler never reads ownership. The internal
    # metrics + execution-mode admin routes read through this.
    runtime_ownership: "RuntimeOwnershipPort | None" = None
    execution_mode_transition: "ExecutionModeTransitionService | None" = None
    character_ttl_reaper: CharacterTtlReaper | None = None
    character_freeze_reaper: "CharacterFreezeReaper | None" = None
    # Cloud→Core subscription-lapse batch freeze/thaw, invoked by the
    # internal service-to-service route on tenant tier changes.
    subscription_freeze_service: "SubscriptionFreezeService | None" = None
    # Cloud→Core tenant-tier push, invoked by the internal route so a tier
    # change takes effect without waiting for the operator to re-login.
    cloud_tenant_tier_sync_service: "CloudTenantTierSyncService | None" = None
    # Cloud→Core per-card exclusive-freeze (D7 / EC10-A/EC10-B), invoked by
    # the internal route when an official card's IP-partner contract ends
    # or resumes. Independent of ``subscription_freeze_service`` above —
    # keyed by card, not tenant.
    exclusive_card_freeze_service: "ExclusiveCardFreezeService | None" = None
    subscription_access_guard: "SubscriptionAccessGuard | None" = None
    cloud_subscription_repository: "CloudSubscriptionRepositoryPort | None" = None
    # Display-only hosted credit balance proxy (U3). Wired in cloud mode only;
    # ``None`` leaves ``GET /api/v1/cloud/credits`` degraded, never blocking play.
    cloud_credit_service: "CloudCreditService | None" = None
    # Public hosted price list proxy (AP3). Cloud mode only; ``None`` leaves
    # ``GET /api/v1/cloud/pricing`` degraded rather than quoting stale numbers.
    cloud_pricing_service: "CloudPricingService | None" = None
    cloud_tier_pricing_service: "CloudTierPricingService | None" = None
    # Notice-board unread flag (AN1) behind the in-game dot. None on self-host,
    # where there is no Cloud board to have unread notices on.
    cloud_announcement_service: "CloudAnnouncementService | None" = None
    # Action-level credit charging (AP2). Always present — the null object
    # covers self-host and every tier still on token billing — so instrumented
    # entry points never branch on wiring.
    action_billing_service: (
        "CloudActionBillingService | NullActionBillingService | None"
    ) = None
    # Player-authorised quota overage (AP4). Always wired; the settings routes
    # that read it still 404 outside cloud mode.
    quota_overage_service: "QuotaOverageService | None" = None
    # Display-only mirror of the runtime ceilings the services already enforce
    # (character slots, daily creates, daily 起幕, session cap, capability
    # flags). Always wired — the profile it reads is permissive in self-host,
    # so it reports "no limits" there — and the route that exposes it is
    # mounted in cloud mode only.
    player_runtime_limits_service: "PlayerRuntimeLimitsService | None" = None
    # Hosted player locale / location lifecycle (G2). Cloud mode only — ``None``
    # in self-host, where the locale routes 404 by construction.
    player_locale_service: "PlayerLocaleService | None" = None
    # City-name search behind ``GET /api/v1/geo/search`` (G2). Cloud mode only;
    # ``None`` degrades the picker to an empty result list, never an error.
    geocoding_client: "GeocodingPort | None" = None
    # Read-only external-chat roster projection (LH2), served by the internal
    # service-credential route for the hosted LINE official-channel Cloud side.
    external_chat_roster_service: "ExternalChatRosterService | None" = None
    # Service-credential attachment ingest (LH2): Core-side MIME sniff, size,
    # and pixel-bomb gate before an inbound LINE image lands in Object Storage.
    external_chat_attachment_service: (
        "ExternalChatAttachmentService | None"
    ) = None
    # Recoverable external-chat turn orchestrator (LH2, DR-LH0-004): the durable
    # state machine behind the internal ``POST .../external-chat/turns`` route.
    external_chat_turn_service: "ExternalChatTurnService | None" = None
    # Exposed for the admin character-freeze surface (site-wide overview
    # + immediate freeze/unfreeze) which needs list / get / set_frozen.
    character_repository: "CharacterRepositoryPort | None" = None
    #: Aggregate character census for the Cloud admin dashboard's load card
    #: (``/cloud/stats/characters``). ``None`` without a database — the route
    #: then answers 503 rather than inventing a zero.
    character_activity_stats_service: "CharacterActivityStatsService | None" = None
    proactive_event_bus: ProactiveEventBus | None = None
    feed_post_repository: FeedPostRepositoryPort | None = None
    feed_reaction_repository: FeedReactionRepositoryPort | None = None
    feed_reaction_service: "FeedReactionService | None" = None
    feed_comment_repository: FeedCommentRepositoryPort | None = None
    feed_comment_service: "FeedCommentService | None" = None
    feed_composer_service: FeedComposerService | None = None
    #: CV4 deferred video pipeline. Exposed so the CV6 internal trigger (and
    #: any future admin surface) can drive the same object the two carriers do
    #: rather than re-assembling one.
    feed_video_job_service: "FeedVideoJobService | None" = None
    pending_feed_video_repository: PendingFeedVideoRepositoryPort | None = None
    feed_comment_reply_service: FeedCommentReplyService | None = None
    feed_reaction_memorializer: FeedReactionMemorializer | None = None
    feed_event_bus: FeedEventBus | None = None
    # Phase 4 realtime outbox (§7.1). All three are set ONLY under
    # YURALUME_REALTIME_BACKEND=postgres with a database, and only on the api
    # reader role: ``realtime_outbox`` + ``realtime_rehydrator`` back the SSE
    # ``Last-Event-ID`` replay path (read via ``getattr`` in api/routes/events),
    # and ``realtime_dispatcher`` tails the outbox onto the local buses (started
    # / stopped by the app lifespan). ``None`` on the self-host default (memory
    # backend) and on the background writer role — the red line is that an unset
    # backend changes nothing.
    realtime_outbox: "RealtimeOutboxPort | None" = None
    realtime_rehydrator: "RealtimeEventRehydrator | None" = None
    realtime_dispatcher: "RealtimeEventDispatcher | None" = None
    tts_service: TTSService | None = None
    tts_pregeneration_service: TTSPregenerationService | None = None
    tts_voice_catalog: TTSVoiceCatalogPort | None = None
    fusion_story_repository: FusionStoryRepositoryPort | None = None
    fusion_story_service: FusionStoryService | None = None
    fusion_material_stats_service: FusionMaterialStatsService | None = None
    fusion_to_arc_draft_service: FusionToArcDraftService | None = None
    branching_drama_service: "BranchingDramaService | None" = None
    drama_to_arc_draft_service: DramaToArcDraftService | None = None
    """BD7 — 分歧劇場結局頁的「把這條路寫成劇本」. ``None`` on a rig
    without a branching-drama service, where the route answers 503."""
    studio_job_repository: StudioJobRepositoryPort | None = None
    studio_job_recovery_service: StudioJobRecoveryService | None = None
    # CB2 — durable ``.lumebackup`` export jobs + orchestration. Both are
    # ``None`` without a database: the export is a whole-history dump, so
    # a DB-less dev rig has nothing meaningful to export and the route
    # answers 503.
    character_backup_job_repository: (
        CharacterBackupJobRepositoryPort | None
    ) = None
    character_backup_export_service: (
        CharacterBackupExportService | None
    ) = None
    # CB3 — the restore half; ``None`` without a database for the same
    # reason as the export half.
    character_backup_import_service: (
        CharacterBackupImportService | None
    ) = None
    world_event_repository: WorldEventRepositoryPort | None = None
    rss_source_repository: RssSourceRepositoryPort | None = None
    character_event_inbox_repository: CharacterEventInboxRepositoryPort | None = None
    rss_ingestion_service: RssIngestionService | None = None
    event_curator_service: EventCuratorService | None = None
    event_seed_dispenser: EventSeedDispenser | None = None
    world_event_scheduler: WorldEventScheduler | None = None
    rss_source_sync_service: RssSourceSyncService | None = None
    pending_follow_up_repository: "PendingFollowUpRepositoryPort | None" = None
    pending_follow_up_dispatcher: "PendingFollowUpDispatcher | None" = None
    operator_persona_service: OperatorPersonaService | None = None
    operator_persona_projection_service: OperatorPersonaProjectionService | None = None
    persona_extraction_service: PersonaExtractionService | None = None
    persona_dream_service: PersonaDreamService | None = None
    persona_curiosity_service: PersonaCuriosityService | None = None
    persona_curiosity_planner: PersonaCuriosityPlannerPort | None = None
    character_relationship_repository: CharacterRelationshipRepositoryPort | None = None
    character_peer_profile_repository: CharacterPeerProfileRepositoryPort | None = None
    character_social_knowledge_service: CharacterSocialKnowledgeService | None = None
    character_encounter_repository: CharacterEncounterRepositoryPort | None = None
    character_encounter_intent_repository: (
        CharacterEncounterIntentRepositoryPort | None
    ) = None
    album_repository: AlbumRepositoryPort | None = None
    turn_record_repository: "TurnRecordRepositoryPort | None" = None
    usage_event_repository: "UsageEventRepositoryPort | None" = None
    emotion_event_repository: "EmotionEventRepositoryPort | None" = None
    # HUMANIZATION_ROADMAP P1 repositories (§3.1–§3.5 audit / read paths).
    disposition_drift_history_repository: "DispositionDriftHistoryRepositoryPort | None" = None
    self_reflection_repository: "SelfReflectionRepositoryPort | None" = None
    # Player-side memoir aggregation.
    memory_repository: "MemoryRepositoryPort | None" = None
    memoir_pin_repository: "MemoirPinRepositoryPort | None" = None
    memoir_service: "MemoirService | None" = None
    behavioral_pattern_repository: "BehavioralPatternRepositoryPort | None" = None
    deferred_intent_repository: "DeferredIntentRepositoryPort | None" = None
    # HUMANIZATION_ROADMAP §4.5 — runtime-mutable global settings.
    runtime_settings_repository: "RuntimeSettingsRepositoryPort | None" = None
    provider_connection_repository: "ProviderConnectionRepositoryPort | None" = None
    provider_connection_service: "ProviderConnectionService | None" = None
    quiet_hours_service: "QuietHoursService | None" = None
    # HUMANIZATION_ROADMAP §4.2 — observed register / address preference.
    address_preference_repository: "OperatorAddressPreferenceRepositoryPort | None" = None
    address_preference_service: "AddressPreferenceObserverService | None" = None
    # Player-declared identity / world premise (PP series).
    player_persona_note_repository: "PlayerPersonaNoteRepositoryPort | None" = None
    player_persona_note_service: "PlayerPersonaNoteService | None" = None
    # Per-pair rename log + names edit.
    address_change_log_repository: "AddressChangeLogRepositoryPort | None" = None
    relationship_names_service: "RelationshipNamesService | None" = None
    # HUMANIZATION_ROADMAP §4.6 — A/B framework.
    experiment_repository: "ExperimentRepositoryPort | None" = None
    experiment_assignment_repository: "ExperimentAssignmentRepositoryPort | None" = None
    experiment_service: "ExperimentService | None" = None
    experiment_overlay_service: "ExperimentOverlayService | None" = None
    experiment_analysis_service: "ExperimentAnalysisService | None" = None
    # HUMANIZATION_ROADMAP §4.5 — shared LLM serialisation gate.
    llm_priority_gate: "LLMSerialisationGate | None" = None
    # HUMANIZATION_ROADMAP §4.4 / §4.1 — read-only flag display in UI.
    app_settings: "AppSettings | None" = None
    clock: ClockPort | None = None
    # HOSTED_CORE_SCALING §9.1 — the single shared async engine built once
    # in ``build_container``'s DB branch. ``None`` for in-memory builds and
    # bare ``ServiceContainer()`` test harnesses. Disposed once in the app
    # lifespan shutdown.
    db_engine: "AsyncEngine | None" = None


_RepoBundle = tuple[
    CharacterRepositoryPort,
    ConversationRepositoryPort,
    MemoryRepositoryPort,
    StateHistoryRepositoryPort,
    GoalRepositoryPort,
    ScheduleRepositoryPort,
    MessagingAccountRepositoryPort,
    ChannelBindingRepositoryPort,
    ProactiveAttemptRepositoryPort,
    ToolInvocationRepositoryPort,
    StorySeedRepositoryPort,
    StoryEventRepositoryPort,
    StoryArcRepositoryPort,
    AlbumRepositoryPort,
    "TurnJournalRepositoryPort",
    FeedPostRepositoryPort,
    FeedReactionRepositoryPort,
    FeedCommentRepositoryPort,
]


def _env_int(name: str, default: int) -> int:
    """Read a non-negative int env override, falling back to ``default``.

    Used for the Phase 5 ``YURALUME_``-prefixed knobs (capability caps, reconcile
    interval, reseed jitter). A missing / malformed value keeps the default so
    the self-host path never needs to set these."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        parsed = int(raw.strip())
    except ValueError:
        return default
    return parsed if parsed >= 0 else default


def _capability_caps() -> dict[str, int]:
    """The deployment's §5 per-capability background ceilings.

    One reader for two consumers: the worker's claim filter (what the cap
    actually enforces) and the promise tool loop (which must not offer a
    tool whose queue the operator closed with a cap of 0 — the job would be
    minted and never claimable). Two copies of this env read would be two
    places for those answers to disagree."""
    return {
        "llm": _env_int("YURALUME_BG_CAP_LLM", DEFAULT_CAPABILITY_CAPS["llm"]),
        "image": _env_int(
            "YURALUME_BG_CAP_IMAGE", DEFAULT_CAPABILITY_CAPS["image"],
        ),
        # CV4: bounds concurrent pollers, not GPUs — deliberately far
        # above the image cap, which a video poll must never share.
        "video_poll": _env_int(
            "YURALUME_BG_CAP_VIDEO_POLL",
            DEFAULT_CAPABILITY_CAPS["video_poll"],
        ),
    }


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean env override, falling back to ``default``.

    Same fail-to-default contract as :func:`_env_int`: an unset or
    unrecognised value keeps the default, so a deployment that never heard
    of the knob behaves exactly as it did before the knob existed."""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _build_in_memory_repositories() -> _RepoBundle:
    return (
        InMemoryCharacterRepository(),
        InMemoryConversationRepository(),
        InMemoryMemoryRepository(),
        InMemoryStateHistoryRepository(),
        InMemoryGoalRepository(),
        InMemoryScheduleRepository(),
        InMemoryMessagingAccountRepository(),
        InMemoryChannelBindingRepository(),
        InMemoryProactiveAttemptRepository(),
        InMemoryToolInvocationRepository(),
        InMemoryStorySeedRepository(),
        InMemoryStoryEventRepository(),
        InMemoryStoryArcRepository(),
        InMemoryAlbumRepository(),
        InMemoryTurnJournalRepository(),
        InMemoryFeedPostRepository(),
        InMemoryFeedReactionRepository(),
        InMemoryFeedCommentRepository(),
    )


def _build_db_repositories(
    session_factory: "sessionmaker[AsyncSession]",
) -> _RepoBundle:
    from kokoro_link.infrastructure.persistence.sa_channel_binding_repository import (
        SAChannelBindingRepository,
    )
    from kokoro_link.infrastructure.persistence.sa_character_repository import SACharacterRepository
    from kokoro_link.infrastructure.persistence.sa_conversation_repository import SAConversationRepository
    from kokoro_link.infrastructure.persistence.sa_goal_repository import SAGoalRepository
    from kokoro_link.infrastructure.persistence.sa_memory_repository import SAMemoryRepository
    from kokoro_link.infrastructure.persistence.sa_messaging_account_repository import (
        SAMessagingAccountRepository,
    )
    from kokoro_link.infrastructure.persistence.sa_proactive_attempt_repository import (
        SAProactiveAttemptRepository,
    )
    from kokoro_link.infrastructure.persistence.sa_schedule_repository import SAScheduleRepository
    from kokoro_link.infrastructure.persistence.sa_state_history_repository import SAStateHistoryRepository
    from kokoro_link.infrastructure.persistence.sa_tool_invocation_repository import (
        SAToolInvocationRepository,
    )
    from kokoro_link.infrastructure.persistence.sa_story_repositories import (
        SAStoryEventRepository,
        SAStorySeedRepository,
    )
    from kokoro_link.infrastructure.persistence.sa_story_arc_repository import (
        SAStoryArcRepository,
    )
    from kokoro_link.infrastructure.persistence.sa_album_repository import (
        SAAlbumRepository,
    )
    from kokoro_link.infrastructure.persistence.sa_turn_journal_repository import (
        SaTurnJournalRepository,
    )
    from kokoro_link.infrastructure.persistence.sa_feed_post_repository import (
        SAFeedPostRepository,
    )
    from kokoro_link.infrastructure.persistence.sa_feed_comment_repository import (
        SAFeedCommentRepository,
    )
    from kokoro_link.infrastructure.persistence.sa_feed_reaction_repository import (
        SAFeedReactionRepository,
    )

    return (
        SACharacterRepository(session_factory),
        SAConversationRepository(session_factory),
        SAMemoryRepository(session_factory),
        SAStateHistoryRepository(session_factory),
        SAGoalRepository(session_factory),
        SAScheduleRepository(session_factory),
        SAMessagingAccountRepository(session_factory),
        SAChannelBindingRepository(session_factory),
        SAProactiveAttemptRepository(session_factory),
        SAToolInvocationRepository(session_factory),
        SAStorySeedRepository(session_factory),
        SAStoryEventRepository(session_factory),
        SAStoryArcRepository(session_factory),
        SAAlbumRepository(session_factory),
        SaTurnJournalRepository(session_factory),
        SAFeedPostRepository(session_factory),
        SAFeedReactionRepository(session_factory),
        SAFeedCommentRepository(session_factory),
    )


def _build_messaging_adapters(
    object_storage: ObjectStoragePort | None = None,
) -> dict[Platform, ChannelAdapterPort]:
    """Stateless adapter instances keyed by platform.

    Credentials travel inside ``OutboundMessage.credentials`` per-call
    (sourced from the ``MessagingAccount`` the dispatcher is operating
    on), so one adapter instance serves every account on that platform.
    """
    telegram_fetcher = (
        _build_telegram_local_image_fetcher(object_storage)
        if object_storage is not None
        else None
    )
    return {
        Platform.TELEGRAM: TelegramAdapter(local_image_fetcher=telegram_fetcher),
        Platform.LINE: LineAdapter(),
        Platform.DISCORD: DiscordAdapter(),
        Platform.WHATSAPP: WhatsAppAdapter(),
    }


def _build_telegram_local_image_fetcher(object_storage: ObjectStoragePort):
    async def fetch(url: str) -> LocalImageFetchResult:
        object_key = _object_key_from_core_public_url(url)
        if object_key is None:
            object_key = object_storage.object_key_from_url(url)
        if object_key is None:
            return LocalImageFetchResult(handled=False)
        try:
            content = await object_storage.get_bytes(object_key=object_key)
        except ObjectNotFoundError:
            _LOGGER.warning(
                "Telegram local image fetch failed: object not found url=%s key=%s",
                url, object_key,
            )
            return LocalImageFetchResult(handled=True)
        except ObjectStorageError:
            _LOGGER.exception(
                "Telegram local image fetch failed url=%s key=%s",
                url, object_key,
            )
            return LocalImageFetchResult(handled=True)
        return LocalImageFetchResult(handled=True, content=content)

    return fetch


def _object_key_from_core_public_url(url: str) -> str | None:
    prefix = "/v1/public/"
    if url.startswith(prefix):
        raw_key = url[len(prefix):]
    else:
        parsed = urlparse(url)
        if not parsed.path.startswith(prefix):
            return None
        raw_key = parsed.path[len(prefix):]
    raw_key = unquote(raw_key).split("?", 1)[0].split("#", 1)[0]
    return raw_key or None


def _build_post_turn_processor(
    *,
    registry: ChatModelRegistryPort,
    default_provider_id: str,
    active_provider: ActiveLLMProviderPort | None = None,
    local_tz: tzinfo = timezone.utc,
) -> PostTurnProcessorPort:
    """Pick a post-turn processor based on available providers.

    When a real default provider is configured we wire the LLM-backed
    processor with an ``active_provider`` reference so it honours the
    frontend's per-call model pick — memory extraction follows whatever
    the operator picked in the UI, not whatever the env file said at
    boot. Fake / unresolvable providers keep getting the null processor
    (no garbage memories written).
    """
    if active_provider is None:
        return NullPostTurnProcessor()
    return LLMPostTurnProcessor(
        provider=active_provider,
        feature_key=FEATURE_POST_TURN,
        local_tz=local_tz,
    )


def _build_goal_reviewer(
    *,
    registry: ChatModelRegistryPort,
    default_provider_id: str,
    active_provider: ActiveLLMProviderPort | None = None,
    local_tz: tzinfo = timezone.utc,
) -> GoalReviewerPort:
    if active_provider is None:
        return NullGoalReviewer()
    return LLMGoalReviewer(
        provider=active_provider,
        feature_key=FEATURE_GOAL_REVIEW,
        # Site-level fallback only — a caller that resolved the owning
        # operator's zone passes it per-review.
        local_tz=local_tz,
    )


def _build_story_expander(
    *,
    registry: ChatModelRegistryPort,
    default_provider_id: str,
    active_provider: ActiveLLMProviderPort | None = None,
    cloud_mode: bool = False,
):
    """Pick an expander based on available providers.

    Fake-provider deployments get ``NullStoryEventExpander`` (plain
    single-sentence narrative). Real providers get the LLM-backed
    expander wired to the UI-selected model.
    """
    if active_provider is None:
        return NullStoryEventExpander()
    return LLMStoryEventExpander(
        provider=active_provider,
        feature_key=FEATURE_STORY_EXPAND,
        cloud_mode=cloud_mode,
    )


def _build_story_arc_planner(
    *,
    registry: ChatModelRegistryPort,
    default_provider_id: str,
    active_provider: ActiveLLMProviderPort | None = None,
) -> StoryArcPlannerPort:
    """Arc planner follows the same fake/real split as expander.

    The ``NullStoryArcPlanner`` produces a template arc so the service
    always has a real arc to persist even when no LLM is wired — tests
    and fake-provider dev exercise the same ``ensure_active_arc`` flow.
    """
    if active_provider is None:
        return NullStoryArcPlanner()
    return LLMStoryArcPlanner(
        provider=active_provider, feature_key=FEATURE_ARC_PLAN,
    )


def _build_story_arc_season_decider(
    *,
    active_provider: ActiveLLMProviderPort | None = None,
):
    """Pick the dormant story-arc season opener decider."""
    if active_provider is None:
        return NullStoryArcSeasonDecider()
    return LLMStoryArcSeasonDecider(
        provider=active_provider, feature_key=FEATURE_ARC_SEASON_DECIDE,
    )


def _build_story_beat_rechecker(
    *,
    active_provider: ActiveLLMProviderPort | None = None,
):
    """Pick the repeated beat-attempt semantic rechecker."""
    if active_provider is None:
        return NullStoryBeatRechecker()
    return LLMStoryBeatRechecker(
        provider=active_provider,
        feature_key=FEATURE_ARC_BEAT_RECHECK,
    )


def _build_arc_completion_memory_writer(
    *,
    active_provider: ActiveLLMProviderPort | None = None,
):
    """Pick the completed-arc relationship milestone writer."""
    if active_provider is None:
        return None
    return LLMArcCompletionMemoryWriter(
        provider=active_provider,
        feature_key=FEATURE_ARC_COMPLETION_MEMORY,
    )


def _build_proactive_decider(
    *,
    active_provider: ActiveLLMProviderPort | None,
):
    """Pick the proactive decider.

    The ``fake`` provider can't write coherent first-person judgement
    about whether to speak, so we stick with the null decider there.
    Any real provider gets the LLM-backed decider.
    """
    if active_provider is None:
        return NullProactiveDecider()
    return LLMProactiveDecider(provider=active_provider)


def _build_proactive_intention_judge(
    *,
    active_provider: ActiveLLMProviderPort | None,
    default_provider_id: str,
):
    """Pick the proactive intention judge.

    This layer needs natural-language self-scrutiny and structured JSON,
    so the fake provider must not run it. Real deployments route it
    through the active provider so operators can pin a stronger model via
    ``FEATURE_PROACTIVE_INTENTION``.
    """
    if active_provider is None:
        return NullProactiveIntentionJudge()
    return LLMProactiveIntentionJudge(
        provider=active_provider,
        feature_key=FEATURE_PROACTIVE_INTENTION,
    )


def _build_embedder(
    *,
    settings: AppSettings,
    identity_resolver: CloudOperatorIdentityResolver | None = None,
    routing_profile_port: CloudRoutingProfilePort | None = None,
    provider_credentials_enabled: bool = True,
) -> EmbedderPort:
    """Install the mode-specific embedder behind a stable runtime reference."""
    if settings.use_embedder:
        if settings.cloud.active:
            if not provider_credentials_enabled:
                return RuntimeConfigurableEmbedder(
                    NullEmbedder(dimension=settings.embedding.dimension),
                )
            return RuntimeConfigurableEmbedder(
                CloudGatewayEmbedder(
                    base_url=settings.cloud.gateway_url,
                    deployment_token=settings.cloud.deployment_token,
                    deployment_id=settings.cloud.deployment_id,
                    audience=settings.cloud.deployment_audience,
                    default_model=(settings.cloud.embedding_preset or settings.embedding.model),
                    dimension=settings.embedding.dimension,
                    identity_resolver=identity_resolver,
                    routing_profile_port=routing_profile_port,
                ),
            )
        return RuntimeConfigurableEmbedder(
            LMStudioEmbedder(
                base_url=settings.embedding.base_url,
                api_key=settings.embedding.api_key,
                model=settings.embedding.model,
                dimension=settings.embedding.dimension,
            ),
        )
    return RuntimeConfigurableEmbedder(NullEmbedder(dimension=settings.embedding.dimension))


def _build_memory_consolidator(
    *,
    registry: ChatModelRegistryPort,
    default_provider_id: str,
    active_provider: ActiveLLMProviderPort | None = None,
) -> MemoryConsolidatorPort:
    """Pick a consolidator based on the configured provider. The LLM
    variant needs a real model that honours the JSON-output rules in
    the prompt; ``fake`` provider gets a null consolidator so the
    pipeline falls through to decay-only behaviour.
    """
    if active_provider is None:
        return NullMemoryConsolidator()
    return LLMMemoryConsolidator(
        provider=active_provider, feature_key=FEATURE_MEMORY_CONSOLIDATE,
    )


def _build_dialogue_summarizer(
    *,
    registry: ChatModelRegistryPort,
    default_provider_id: str,
    active_provider: ActiveLLMProviderPort | None = None,
) -> DialogueSummarizerPort:
    """Pick a dialogue summarizer based on the configured provider.

    Schedule / arc / proactive generators each run this pass before
    their own prompt so they can cite "what's currently being talked
    about" without pasting the raw transcript. ``fake`` provider (and
    any unresolvable provider) gets the null summarizer — callers
    treat empty output as "no context" and skip the section.
    """
    if active_provider is None:
        return NullDialogueSummarizer()
    return LLMDialogueSummarizer(
        provider=active_provider, feature_key=FEATURE_DIALOGUE_SUMMARY,
    )


def _build_nsfw_safe_summarizer(
    *,
    active_provider: ActiveLLMProviderPort | None = None,
) -> NsfwSafeSummaryPort:
    if active_provider is None:
        return NullNsfwSafeSummarizer()
    return LLMNsfwSafeSummarizer()


def _build_schedule_planner(
    *,
    registry: ChatModelRegistryPort,
    default_provider_id: str,
    active_provider: ActiveLLMProviderPort | None = None,
) -> SchedulePlannerPort:
    """Pick a planner that matches the configured provider.

    - ``fake`` → deterministic stub with weekday/weekend templates
    - real provider → LLM planner (routed through the UI-selected model)
    - provider missing / unresolvable → null planner (empty schedule)
    """
    if active_provider is None:
        return StubSchedulePlanner()
    return LLMSchedulePlanner(
        provider=active_provider, feature_key=FEATURE_SCHEDULE_PLAN,
    )


# The three "real world" adapters are built from a single site-settings value
# object each, so their builders live in ``site_settings_providers`` where the
# hot-reload wrappers can share them verbatim. These thin ``AppSettings``-shaped
# wrappers stay for the existing call sites.


def _build_calendar_provider(
    *, settings: AppSettings, local_tz: tzinfo,
) -> CalendarContextPort:
    return build_calendar_provider(settings.calendar, local_tz=local_tz)


def _build_weather_provider(*, settings: AppSettings) -> WeatherContextPort:
    return build_weather_provider(
        settings.weather,
        default_primary_language=settings.default_primary_language,
    )


def _build_geo_location_provider(*, settings: AppSettings) -> GeoLocationPort:
    return build_geo_location_provider(settings.geoip)


def _resolve_local_tz(settings: UserTimezoneSettings) -> tzinfo:
    """Default user timezone for civil-date boundaries.

    Server clocks and persistence remain UTC. Civil dates use this
    explicit setting until per-user timezone persistence lands.
    """
    timezone_id = settings.default_timezone_id
    return timezone_for_id(timezone_id)


def _build_image_profile_registry(
    *,
    settings: AppSettings,
    registry: ChatModelRegistryPort,
    active_provider: ActiveLLMProviderPort | None = None,
) -> ImageProfileRegistry:
    """Materialise the operator-defined image profile list.

    Sources profiles from ``KOKORO_IMAGE_PROFILES`` (JSON file or
    inline list). When unset, falls back to a simple external API
    profile synthesised from ``KOKORO_IMAGE_API_*``.

    Legacy local ComfyUI profiles can still be declared explicitly in
    ``KOKORO_IMAGE_PROFILES``; they are no longer inferred from global
    env vars.
    """
    rewriter: LLMPromptRewriter | None = None
    if active_provider is not None:
        rewriter = LLMPromptRewriter(
            provider=active_provider, feature_key=FEATURE_PROMPT_REWRITE,
        )

    default_api = (
        ExternalImageApiProfileConfig(
            base_url=settings.image_api.base_url,
            api_key=settings.image_api.api_key,
            model=settings.image_api.model,
            provider=settings.image_api.provider,
            timeout_seconds=settings.image_api.timeout_seconds,
        )
        if settings.image_api.enabled
        else None
    )
    profiles = load_image_profiles(
        raw_config=settings.image_profiles_raw,
        default_api=default_api,
    )
    return ImageProfileRegistry(profiles, prompt_rewriter=rewriter)


def _build_video_profile_registry(
    *,
    settings: AppSettings,
) -> VideoProfileRegistry:
    """Materialise operator-defined video profiles.

    Empty ``KOKORO_VIDEO_PROFILES`` can still create a default
    ``external_api`` profile from ``KOKORO_VIDEO_API_*``.
    """
    default_api = (
        ExternalVideoApiProfileConfig(
            base_url=settings.video_api.base_url,
            api_key=settings.video_api.api_key,
            model=settings.video_api.model,
            provider=settings.video_api.provider,
            timeout_seconds=settings.video_api.timeout_seconds,
        )
        if settings.video_api.enabled
        else None
    )
    profiles = load_video_profiles(
        raw_config=settings.video_profiles_raw,
        default_api=default_api,
    )
    return VideoProfileRegistry(profiles)


def _build_object_storage(settings: AppSettings) -> ObjectStoragePort:
    """The process's object-storage port, with image variants wired in.

    Every producer and deleter of image bytes receives this one instance,
    so wrapping the adapter here — and only here — is what makes WebP
    variant generation unskippable across all of them (plan D3). No call
    site changes, and no knob: a deployment either has variants for its
    images or falls back to originals per-request, both of which work.
    """
    return VariantAwareObjectStorage(_build_storage_adapter(settings))


def _build_character_data_erasure(
    *,
    db_session_factory: "sessionmaker[AsyncSession] | None",
    object_storage: ObjectStoragePort,
    character_repository: CharacterRepositoryPort,
    repositories: "Mapping[str, object]",
) -> "CharacterDataErasureService":
    """The one erasure engine behind ``delete_character`` (CD2).

    With a database, the boundary is derived from the CB0 registry and
    the ORM metadata — the only mode that can promise zero orphan rows.
    Without one, the sweep can only reach the repositories this container
    built; ``RepositoryCharacterDatabaseEraser``'s docstring records what
    that mode does not cover.

    ``repositories`` is keyed by
    ``CHARACTER_ERASURE_REPOSITORY_SLOTS`` — the single declaration of
    which roles the in-memory boundary contains and in which order they
    run. An unregistered key fails loudly at construction.
    """
    from kokoro_link.application.services.character_data_erasure import (
        CharacterDataErasureService,
        RepositoryCharacterDatabaseEraser,
    )

    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_character_data_eraser import (
            SACharacterDataEraser,
        )
        database_eraser = SACharacterDataEraser(db_session_factory)
    else:
        database_eraser = RepositoryCharacterDatabaseEraser(
            character_repository=character_repository,
            repositories=repositories,
        )
    return CharacterDataErasureService(
        database_eraser=database_eraser,
        object_storage=object_storage,
    )


def _build_character_data_reset(
    *,
    db_session_factory: "sessionmaker[AsyncSession] | None",
    memory_repository: "MemoryRepositoryPort | None",
    conversation_repository: "ConversationRepositoryPort | None",
    state_history_repository: "StateHistoryRepositoryPort | None",
    operator_persona_repository: "OperatorPersonaRepositoryPort | None",
) -> "CharacterResetEraserPort":
    """The one erasure engine behind ``reset_character_data`` (CD3).

    With a database, each flag's table set is resolved from the RESET
    consumer policy and swept in one transaction, same as the CD2 delete
    engine. Without one, each flag is delegated to the one repository
    this container already built for it.

    Both modes are wrapped in the availability gate: a flag whose
    subsystem this deployment never built (``KOKORO_PERSONA_ENABLED=false``
    leaves ``operator_persona_repository`` unset) stays the no-op it was
    before CD3, instead of the SQL eraser hard-deleting a table the
    deployment does not otherwise use. The repository map is the one
    place that knows which subsystems exist, so it feeds both the
    fallback eraser and the gate.
    """
    from kokoro_link.application.dto.character_backup.consumer_policies import (
        ResetFlag,
    )
    from kokoro_link.application.services.character_reset import (
        FlagGatedCharacterResetEraser,
        RepositoryCharacterResetEraser,
    )

    repositories = {
        ResetFlag.MEMORIES: memory_repository,
        ResetFlag.CONVERSATIONS: conversation_repository,
        ResetFlag.STATE_HISTORY: state_history_repository,
        ResetFlag.OPERATOR_PERSONA: operator_persona_repository,
    }
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_character_reset_eraser import (
            SACharacterResetEraser,
        )
        eraser: "CharacterResetEraserPort" = SACharacterResetEraser(
            db_session_factory,
        )
    else:
        eraser = RepositoryCharacterResetEraser(repositories)
    return FlagGatedCharacterResetEraser(
        eraser,
        available_flags=frozenset(
            flag
            for flag, repository in repositories.items()
            if repository is not None
        ),
    )


def _build_storage_adapter(settings: AppSettings) -> ObjectStoragePort:
    provider = settings.storage.provider
    if provider == "http":
        return HttpObjectStorage(
            base_url=settings.storage.base_url,
            api_key=settings.storage.api_key,
            public_base_url=settings.storage.public_base_url,
            timeout_seconds=settings.storage.timeout_seconds,
        )
    if provider == "memory":
        return InMemoryObjectStorage()
    raise ValueError("KOKORO_STORAGE_PROVIDER must be http")


def _build_runtime_lease_backend(
    app_settings: AppSettings,
    db_session_factory: "sessionmaker[AsyncSession] | None",
) -> "BackgroundCoordinatorLeasePort":
    """The process's :class:`BackgroundCoordinatorLeasePort` for per-key claims.

    Distributed topology → the SA lease on the shared ``background_runtime_leases``
    table, so scaled replicas can coordinate. The signal reuses the existing
    distributed opt-ins: ``postgres`` realtime backend (the api-side Phase 4 split
    signal, present on every reader replica) OR ``postgres`` background backend
    (the P3-C execution opt-in) with a database present. Self-host (both at their
    ``memory``/``embedded`` defaults, or no DB) → an in-process lease, which is
    byte-identical to a process-local lock: a single process always finds the key
    free, so ``acquire`` always succeeds. No new self-host env is introduced.
    """
    process = app_settings.process
    distributed = db_session_factory is not None and (
        process.realtime_backend == "postgres"
        or process.background_backend == "postgres"
    )
    if distributed:
        from kokoro_link.infrastructure.persistence.sa_background_runtime import (
            SABackgroundCoordinatorLease,
        )

        return SABackgroundCoordinatorLease(db_session_factory)
    from kokoro_link.infrastructure.repositories.in_memory_background_jobs import (
        InMemoryBackgroundCoordinatorLease,
    )

    return InMemoryBackgroundCoordinatorLease()


def _runtime_lease_owner_id(prefix: str) -> str:
    """A stable-per-process, unique-across-processes lease owner id.

    ``background_runtime_leases.owner_id`` is ``String(64)``, and a container /
    pod hostname can be long, so the host part is truncated — the ``uuid4``
    suffix is what actually guarantees uniqueness across incarnations.
    """
    import os
    import socket
    from uuid import uuid4

    return f"{prefix}-{socket.gethostname()[:24]}-{os.getpid()}-{uuid4().hex[:8]}"


def _build_studio_execution_lease(
    app_settings: AppSettings,
    db_session_factory: "sessionmaker[AsyncSession] | None",
) -> "StudioExecutionLease":
    """Per-target Creator Studio execution lease (HOSTED_CORE_SCALING §13 Phase 4
    前置 1).

    Distributed topology → SA lease on the shared ``background_runtime_leases``
    table so scaled ``api`` replicas can't double-drive one Studio target. The
    signal reuses the existing distributed opt-ins: ``postgres`` realtime backend
    (the api-side Phase 4 split signal, present on every reader replica) OR
    ``postgres`` background backend (the P3-C execution opt-in) with a database
    present. Self-host (both at their ``memory``/``embedded`` defaults, or no DB)
    → an in-process lease, byte-identical to the historical process-local lock:
    a single process always finds the target free once the lock lets the runner
    in, so ``acquire`` always succeeds. No new self-host env is introduced.
    """
    from kokoro_link.application.services.studio_execution_lease import (
        StudioExecutionLease,
    )

    return StudioExecutionLease(
        _build_runtime_lease_backend(app_settings, db_session_factory),
        owner_id=_runtime_lease_owner_id("studio"),
    )


def _build_scene_generator(
    *,
    settings: AppSettings,
) -> ComfySceneGenerator | None:
    if not settings.comfyui.enabled:
        return None
    import pathlib

    workflow_file = (
        pathlib.Path(settings.comfyui.workflow_file)
        if settings.comfyui.workflow_file
        else DEFAULT_WORKFLOW_FILE
    )
    client = AsyncComfyUiClient(
        server=settings.comfyui.server,
        generation_timeout=settings.comfyui.generation_timeout_seconds,
    )
    return ComfySceneGenerator(
        client=client,
        workflow_builder=WorkflowBuilder(workflow_file),
        checkpoint=settings.comfyui.checkpoint or None,
    )


def _build_scene_image_port(
    *,
    settings: AppSettings,
    active_image_provider: ActiveImageProviderPort,
) -> SceneImagePort | None:
    """Pick the branching-drama scene renderer for this deployment.

    Cloud first: a hosted deployment has no local GPU, so before BD1 its
    dramas landed with every node picture-less and nothing said so. It now
    borrows the same active image provider every other hosted image
    surface uses. Self-host keeps the local ComfyUI renderer, wrapped so
    the service sees one port. Neither configured → ``None``, which the
    service already treats as "skip images", unchanged.
    """
    if settings.cloud.active:
        return ActiveProviderSceneImageAdapter(
            image_provider=active_image_provider,
            feature_key=FEATURE_BRANCHING_DRAMA_SCENE,
        )
    generator = _build_scene_generator(settings=settings)
    if generator is None:
        return None
    return ComfySceneImageAdapter(generator)


def _drama_image_prefetch_depth() -> int:
    """How many tree layers of scene art to draw ahead of the player.

    Self-host draws on its own GPU and keeps the historical depth. Hosted
    deployments pay per image for branches most players never walk into,
    so 1 is the recommended setting there; ``0`` switches drama scene art
    off without unwiring the renderer.
    """
    return _env_int(
        "KOKORO_DRAMA_IMAGE_PREFETCH_DEPTH", IMAGE_PREFETCH_DEPTH,
    )


def _build_tool_registry(
    *,
    settings: AppSettings,
    image_provider: ActiveImageProviderPort,
    object_storage: ObjectStoragePort,
    album_service: AlbumService | None = None,
    visual_style_service: VisualGenerationStyleService | None = None,
    action_billing: (
        "CloudActionBillingService | NullActionBillingService | None"
    ) = None,
    cloud_identity_resolver: CloudGatewayIdentityResolverPort | None = None,
    cloud_routing_profile_resolver: CloudRoutingProfilePort | None = None,
    provider_credentials_enabled: bool = True,
) -> ToolRegistryPort:
    """Build the tool registry with all adapters this deployment knows.

    Registers production tools only. Test doubles such as EchoTool and
    FakeImageTool stay importable for unit tests but are not exposed by
    the running app catalogue. An unconfigured tool is simply absent
    from the registry rather than raising on a missing server.
    ``album_service`` is injected into ``ComfyImageTool`` so every
    generated image lands in the operator-browsable album; ``None`` is
    tolerated (tests + early boot) and the tool falls back to returning
    bytes only.
    """
    tools: list[ToolPort] = []
    # The chat tool always registers — resolution decides at call time
    # whether any profile is available. Operators can disable per-
    # character via ``allowed_tools``; registry-level gating would
    # require teardown/restart whenever a profile is added.
    tools.append(
        ComfyImageTool(
            image_provider=image_provider,
            uploads_dir=settings.uploads_dir,
            object_storage=object_storage,
            album_service=album_service,
            visual_style_service=visual_style_service,
            action_billing=action_billing,
        ),
    )
    # web_fetch is always on — zero external dependency besides httpx,
    # already a baseline dep. Per-character gating still applies via
    # ``character.allowed_tools``.
    tools.append(
        WebFetchTool(
            fetcher=HttpxReadabilityFetcher(
                timeout_seconds=settings.web_fetch.timeout_seconds,
                max_html_bytes=settings.web_fetch.max_html_bytes,
                max_text_chars=settings.web_fetch.max_text_chars,
            ),
        ),
    )
    # Hosted mode: Core holds no provider keys (its provider-settings API is
    # 403 in cloud mode), so ``web_search`` can only exist as a Gateway route.
    # This is the *only* branch that mounts it there — ``runtime_sync`` cannot
    # reach it either, because a hosted deployment has no ``search`` rows for it
    # to sync from.
    if settings.cloud.active:
        # A role that holds no deployment token (the coordinator only enqueues
        # durable work) must not construct a Gateway-backed adapter at all —
        # same gate the cloud embedder uses. It never executes a tool call, so
        # an absent ``web_search`` costs it nothing.
        if cloud_identity_resolver is not None and provider_credentials_enabled:
            tools.append(
                WebSearchTool(
                    client=CloudGatewaySearchClient(
                        base_url=settings.cloud.gateway_url,
                        deployment_token=settings.cloud.deployment_token,
                        deployment_id=settings.cloud.deployment_id,
                        audience=settings.cloud.deployment_audience,
                        identity_resolver=cloud_identity_resolver,
                        routing_profile_port=cloud_routing_profile_resolver,
                    ),
                ),
            )
        return InMemoryToolRegistry(tools)
    # Startup wiring for the deprecated ``TAVILY_*`` env path. This is a
    # compatibility bridge only: on first boot the same env is also seeded
    # into a DB ``search`` connection row (see ``_legacy_provider_drafts``),
    # after which ``runtime_sync._sync_search_tool`` becomes the source of
    # truth and hot-replaces this ``web_search`` from DB state. Building it
    # here as well means the tool exists before the first sync runs.
    if settings.tavily.enabled:
        tavily_client = TavilyClient(
            api_key=settings.tavily.api_key,
            base_url=settings.tavily.base_url,
            search_depth=settings.tavily.search_depth,
            timeout_seconds=settings.tavily.timeout_seconds,
        )
        tools.append(
            WebSearchTool(
                client=tavily_client,
                default_max_results=settings.tavily.max_results,
            ),
        )
    return InMemoryToolRegistry(tools)


def _build_character_draft_generator(
    *,
    active_provider: ActiveLLMProviderPort | None,
) -> CharacterDraftGeneratorPort:
    if active_provider is None:
        return StubCharacterDraftGenerator()
    return LLMCharacterDraftGenerator(
        provider=active_provider,
        feature_key=FEATURE_CHARACTER_DRAFT,
    )


def _build_companion_draft_generator(
    *,
    active_provider: ActiveLLMProviderPort | None,
) -> CompanionDraftGeneratorPort:
    if active_provider is None:
        return StubCompanionDraftGenerator()
    return LLMCompanionDraftGenerator(
        provider=active_provider,
        feature_key=FEATURE_CHARACTER_DRAFT,
    )


def _build_shadow_runtime(
    *,
    app_settings: AppSettings,
    db_session_factory: "sessionmaker[AsyncSession] | None",
    character_repository: CharacterRepositoryPort,
    operator_profile_repository: OperatorProfileRepositoryPort | None,
    subscription_access_guard: "SubscriptionAccessGuard | None",
    clock: ClockPort,
):
    """Build the P2-B shadow runtime (HOSTED_CORE_SCALING §13 Phase 2).

    Gate is split (M6): whenever ``YURALUME_BACKGROUND_SHADOW=postgres`` AND a
    DB session factory exists, the durable **queue + lease read ports** are
    built in EVERY role so the admin diagnostics + internal metrics surfaces
    (which live on the api / all role) can read live queue stats and the
    coordinator lease — an api process that only serves routes still needs to
    SEE the queue. The coordinator/worker TASKS are built per the §2.1 matrix:
    the coordinator where ``run_background_coordinator`` (all / background /
    dedicated ``coordinator`` role), the worker where ``run_background_worker``
    (all / background / dedicated ``worker`` role). The embedded tick journal is
    built alongside the coordinator (only the embedded+shadow mirror path reads
    it). A role that runs neither loop (api / connector) gets just the read ports.

    Returns ``(queue, lease, coordinator, worker, tick_journal, bucket_seconds)``.
    ``coordinator`` / ``journal`` are ``None`` on a worker-only role; ``worker``
    is ``None`` on a coordinator-only role; all four trailing values are ``None``
    on api / connector.
    """
    none_row = (None, None, None, None, None, None)
    # P3-C: the durable runtime is built when EITHER the shadow OR the execution
    # backend is on postgres — a hosted execution deployment needs the same
    # queue/lease/coordinator/worker even if it never ran a shadow phase.
    if (
        app_settings.process.background_shadow != "postgres"
        and app_settings.process.background_backend != "postgres"
    ):
        return none_row
    if db_session_factory is None:
        # The only remaining reason to skip when shadow is requested: no DB.
        # (settings.py already fail-fasts shadow=postgres without DATABASE_URL,
        # so this is a defensive belt-and-braces path.)
        _LOGGER.warning(
            "YURALUME_BACKGROUND_SHADOW=postgres set but no database session "
            "factory is available; shadow runtime not built",
        )
        return none_row

    import os
    import socket
    from uuid import uuid4

    from kokoro_link.application.services.background_shadow_coordinator import (
        _DEFAULT_BUCKET_SECONDS,
        ShadowCoordinator,
    )
    from kokoro_link.application.services.background_shadow_worker import (
        ShadowDryRunWorker,
    )
    from kokoro_link.bootstrap.process_roles import matrix_for_role
    from kokoro_link.infrastructure.persistence.sa_background_jobs import (
        SABackgroundJobQueue,
    )
    from kokoro_link.infrastructure.persistence.sa_background_runtime import (
        SABackgroundCoordinatorCursor,
        SABackgroundCoordinatorLease,
        SATickJournal,
    )

    # Queue + lease read ports: built in EVERY role so admin/metrics can read.
    queue = SABackgroundJobQueue(db_session_factory)
    lease = SABackgroundCoordinatorLease(db_session_factory)

    matrix = matrix_for_role(app_settings.process.role)
    # §2.1 dedicated roles decouple the coordinator/worker TASKS from the embedded
    # scheduler: build the coordinator only where the matrix runs the coordinator
    # loop (``all`` / ``background`` transitional, or the dedicated ``coordinator``
    # role), and the worker only where it runs the worker loop (``all`` /
    # ``background`` transitional, or the dedicated ``worker`` role). A role that
    # runs neither (``api`` / ``connector``) still gets the queue+lease READ ports
    # above so admin diagnostics + internal metrics can see the queue.
    if not (matrix.run_background_coordinator or matrix.run_background_worker):
        # api / connector role: serves the diagnostics read ports but runs no
        # coordinator/worker and writes no embedded tick journal. Expected — no
        # warning.
        return (queue, lease, None, None, None, None)

    host = socket.gethostname()
    pid = os.getpid()
    coordinator = None
    journal = None
    if matrix.run_background_coordinator:
        cursor = SABackgroundCoordinatorCursor(db_session_factory)
        # The tick journal only feeds the embedded+shadow mirror path; a
        # dedicated coordinator on the postgres backend never mirrors, but the SA
        # port is a cheap wrapper and ``ShadowCoordinator`` requires the arg.
        journal = SATickJournal(db_session_factory)
        coordinator = ShadowCoordinator(
            queue=queue,
            lease=lease,
            cursor=cursor,
            journal=journal,
            character_repository=character_repository,
            operator_profile_repository=operator_profile_repository,
            owner_id=f"shadow-coord-{host}-{pid}",
            bucket_seconds=_DEFAULT_BUCKET_SECONDS,
            clock=clock,
            mirror_from_journal=(
                app_settings.process.background_shadow == "postgres"
                and app_settings.process.background_backend == "embedded"
            ),
        )
    worker = None
    if matrix.run_background_worker:
        worker = ShadowDryRunWorker(
            queue=queue,
            character_repository=character_repository,
            subscription_access_guard=subscription_access_guard,
            operator_profile_repository=operator_profile_repository,
            worker_id=f"shadow-worker-{host}-{pid}-{uuid4().hex[:8]}",
            clock=clock,
            # §13 per-replica execution concurrency (execution mode only; dry-run
            # stays sequential). Deploy scales replicas × this for global width.
            execution_concurrency=_env_int("YURALUME_BG_EXEC_CONCURRENCY", 2),
        )
    return (queue, lease, coordinator, worker, journal, _DEFAULT_BUCKET_SECONDS)


def build_container(settings: AppSettings | None = None) -> ServiceContainer:
    from kokoro_link.bootstrap.process_roles import matrix_for_role
    app_settings = settings or AppSettings.from_env()
    # Kept for the whole build: the *env-derived* settings are the per-group
    # fallback the reloader re-applies on every refresh. Re-deriving them from
    # the overlaid object would make a cleared DB row resolve to the last DB
    # value instead of the env default.
    env_app_settings = app_settings
    # Site-level runtime settings (Weather/Calendar/GeoIP/NSFW/world-event
    # policy) are DB-authoritative once seeded (CORE_ENV_TO_ADMIN_CONFIG
    # track 2). Read them here, before any provider is wired, and overlay
    # onto app_settings so the whole downstream wiring receives the
    # DB-effective values; env stays the fallback + first-boot seed.
    # Fail-soft: any read error keeps the env-derived settings — and now says
    # so, so /health can tell "no Admin change was made" apart from "this
    # process never managed to read the Admin changes".
    site_settings_source = "env_fallback"
    if app_settings.use_database:
        from kokoro_link.bootstrap.app_runtime_settings_seed import (
            resolve_site_settings_overlay,
        )

        overlay = resolve_site_settings_overlay(app_settings)
        app_settings = overlay.settings
        site_settings_source = overlay.source
    # The four "real world" groups (weather / calendar / geoip / world_events)
    # additionally live behind a hot-swappable holder so a multi-process Hosted
    # fleet converges on an Admin change without a rolling restart
    # The boot overlay above is its
    # initial value; every later value comes from the site-settings refresher.
    site_settings_holder = SiteSettingsHolder(
        SiteSettingsSnapshot.from_app_settings(
            app_settings, source=site_settings_source,
        ),
    )
    process_matrix = matrix_for_role(app_settings.process.role)
    clock = SystemClock()
    object_storage = _build_object_storage(app_settings)

    preferences_repository: PreferencesRepositoryPort
    # HOSTED_CORE_SCALING §9.1 / §13 Phase 1 — ONE async engine + ONE session
    # factory per process. Built once here and threaded to every SA repository
    # below (the old per-site ``build_async_engine`` calls each leaked an
    # undisposed engine + pool). ``None`` on the in-memory fallback path.
    db_engine: "AsyncEngine | None" = None
    db_session_factory: "sessionmaker[AsyncSession] | None" = None
    if app_settings.use_database:
        from kokoro_link.infrastructure.persistence.engine import (
            build_async_engine,
            build_session_factory,
        )
        db_engine = build_async_engine(
            app_settings.database_url,
            pool_size=app_settings.db_pool_size,
            max_overflow=app_settings.db_max_overflow,
        )
        db_session_factory = build_session_factory(db_engine)
        (
            character_repository,
            conversation_repository,
            memory_repository,
            state_history_repository,
            goal_repository,
            schedule_repository,
            messaging_account_repository,
            channel_binding_repository,
            proactive_attempt_repository,
            tool_invocation_repository,
            story_seed_repository,
            story_event_repository,
            story_arc_repository,
            album_repository,
            turn_journal_repository,
            feed_post_repository,
            feed_reaction_repository,
            feed_comment_repository,
        ) = _build_db_repositories(db_session_factory)
        from kokoro_link.infrastructure.persistence.sa_preferences_repository import (
            SAPreferencesRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_operator_profile_repository import (
            SAOperatorProfileRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_initial_relationship_repository import (
            SACharacterOperatorRelationshipSeedRepository,
        )
        preferences_repository = SAPreferencesRepository(db_session_factory)
        operator_profile_repository: OperatorProfileRepositoryPort = (
            SAOperatorProfileRepository(db_session_factory)
        )
        from kokoro_link.infrastructure.persistence.sa_cloud_subscription_repository import (
            SACloudSubscriptionRepository,
        )
        cloud_subscription_repository = SACloudSubscriptionRepository(
            db_session_factory,
        )
        relationship_seed_repository = (
            SACharacterOperatorRelationshipSeedRepository(db_session_factory)
        )
        from kokoro_link.infrastructure.persistence.sa_notifications import (
            SaNotificationPreferencesRepository,
            SaWebPushSubscriptionRepository,
        )
        web_push_subscription_repository = SaWebPushSubscriptionRepository(
            db_session_factory,
        )
        notification_preferences_repository = (
            SaNotificationPreferencesRepository(db_session_factory)
        )
    else:
        (
            character_repository,
            conversation_repository,
            memory_repository,
            state_history_repository,
            goal_repository,
            schedule_repository,
            messaging_account_repository,
            channel_binding_repository,
            proactive_attempt_repository,
            tool_invocation_repository,
            story_seed_repository,
            story_event_repository,
            story_arc_repository,
            album_repository,
            turn_journal_repository,
            feed_post_repository,
            feed_reaction_repository,
            feed_comment_repository,
        ) = _build_in_memory_repositories()
        preferences_repository = InMemoryPreferencesRepository()
        operator_profile_repository = InMemoryOperatorProfileRepository()
        from kokoro_link.infrastructure.repositories.in_memory_cloud_subscription import (
            InMemoryCloudSubscriptionRepository,
        )
        cloud_subscription_repository = InMemoryCloudSubscriptionRepository()
        relationship_seed_repository = (
            InMemoryCharacterOperatorRelationshipSeedRepository()
        )
        web_push_subscription_repository = (
            InMemoryWebPushSubscriptionRepository()
        )
        notification_preferences_repository = (
            InMemoryNotificationPreferencesRepository()
        )

    from kokoro_link.application.services.subscription_access_guard import (
        SubscriptionAccessGuard,
    )
    subscription_access_guard = SubscriptionAccessGuard(
        subscription_repository=cloud_subscription_repository,
        operator_profile_repository=operator_profile_repository,
    )

    # Read-only roster projection for the internal external-chat route (LH2).
    # Wired in every mode: the route's own service-credential gate 503s on
    # self-host (no credentials configured), so exposing the service is safe.
    from kokoro_link.application.services.external_chat_roster_service import (
        ExternalChatRosterService,
    )
    external_chat_roster_service = ExternalChatRosterService(
        character_repository=character_repository,
        operator_profile_repository=operator_profile_repository,
        subscription_access_guard=subscription_access_guard,
        object_storage=object_storage,
    )
    # Attachment ingest reuses the roster projection as its single
    # authorization source (roster membership == chattable), and the operator
    # repository only to recover the owner id for the object key. Wired in
    # every mode; the route's own credential gate 503s where unconfigured.
    from kokoro_link.application.services.external_chat_attachment_service import (
        ExternalChatAttachmentService,
    )
    external_chat_attachment_service = ExternalChatAttachmentService(
        roster_service=external_chat_roster_service,
        operator_profile_repository=operator_profile_repository,
        object_storage=object_storage,
    )

    nsfw_mode_service = NsfwModeService(
        preferences=preferences_repository,
        ttl_seconds=app_settings.nsfw_mode.ttl_seconds,
        clock=clock,
    )

    # Fusion-story repo lives outside the main ``_RepoBundle`` so we can
    # add the table without rippling through every fixture that builds
    # the bundle by destructuring. SA + in-memory share the same
    # ``FusionStoryRepositoryPort`` shape.
    fusion_story_repository: FusionStoryRepositoryPort
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_fusion_story_repository import (
            SAFusionStoryRepository,
        )
        fusion_story_repository = SAFusionStoryRepository(db_session_factory)
    else:
        fusion_story_repository = InMemoryFusionStoryRepository()

    branching_drama_repository: BranchingDramaRepositoryPort
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_branching_drama_repository import (
            SABranchingDramaRepository,
        )
        branching_drama_repository = SABranchingDramaRepository(db_session_factory)
    else:
        from kokoro_link.infrastructure.repositories.in_memory_branching_drama import (
            InMemoryBranchingDramaRepository,
        )
        branching_drama_repository = InMemoryBranchingDramaRepository()

    # Creator Studio durable job ledger (C0) — same isolated-engine
    # pattern as fusion_story / branching_drama above.
    studio_job_repository: StudioJobRepositoryPort
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_studio_job_repository import (
            SAStudioJobRepository,
        )
        studio_job_repository = SAStudioJobRepository(db_session_factory)
    else:
        from kokoro_link.infrastructure.repositories.in_memory_studio_jobs import (
            InMemoryStudioJobRepository,
        )
        studio_job_repository = InMemoryStudioJobRepository()

    # CV4 pending LumeGram video posts — same shape as the studio job ledger:
    # a durable row per in-flight generation, an in-memory twin for a DB-less
    # rig. Note the twin is genuinely usable here (unlike the backup ledger):
    # the whole record is written and read by the same process within
    # minutes, so a restart-less dev box loses nothing by holding it in RAM.
    pending_feed_video_repository: PendingFeedVideoRepositoryPort
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_pending_feed_video_repository import (  # noqa: E501
            SAPendingFeedVideoRepository,
        )
        pending_feed_video_repository = SAPendingFeedVideoRepository(
            db_session_factory,
        )
    else:
        from kokoro_link.infrastructure.repositories.in_memory_pending_feed_videos import (  # noqa: E501
            InMemoryPendingFeedVideoRepository,
        )
        pending_feed_video_repository = InMemoryPendingFeedVideoRepository()

    # CB2 durable backup job ledger + read-only export query layer. Both
    # exist only with a real database (same isolated pattern as the
    # studio job ledger above); the in-memory job twin serves unit tests
    # directly, not this container — a DB-less rig has no character
    # history worth exporting, so the whole feature stays unwired and
    # the API answers 503.
    character_backup_job_repository: CharacterBackupJobRepositoryPort | None = None
    character_backup_export_reader = None
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_character_backup_job_repository import (
            SACharacterBackupJobRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_character_backup_export_reader import (
            SACharacterBackupExportReader,
        )
        character_backup_job_repository = SACharacterBackupJobRepository(
            db_session_factory,
        )
        character_backup_export_reader = SACharacterBackupExportReader(
            db_session_factory,
        )

    # Busy-defer follow-up repo. Same isolated-engine pattern as
    # fusion_story / branching_drama — additions never ripple through
    # the main ``_RepoBundle``.
    pending_follow_up_repository: "PendingFollowUpRepositoryPort"
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_pending_follow_up_repository import (
            SaPendingFollowUpRepository,
        )
        pending_follow_up_repository = SaPendingFollowUpRepository(
            db_session_factory,
        )
    else:
        from kokoro_link.infrastructure.repositories.in_memory_pending_follow_ups import (
            InMemoryPendingFollowUpRepository,
        )
        pending_follow_up_repository = InMemoryPendingFollowUpRepository()

    # External-event pipeline repos (RSS pool + per-character inbox).
    # Same isolated-engine pattern as fusion_story / branching_drama —
    # additions never ripple through the main ``_RepoBundle``.
    world_event_repository: WorldEventRepositoryPort
    rss_source_repository: RssSourceRepositoryPort
    character_event_inbox_repository: CharacterEventInboxRepositoryPort
    character_event_mention_repository: CharacterEventMentionRepositoryPort
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_world_event_repository import (
            SaWorldEventRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_rss_source_repository import (
            SaRssSourceRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_character_event_inbox_repository import (
            SaCharacterEventInboxRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_character_event_mention_repository import (
            SaCharacterEventMentionRepository,
        )
        world_event_repository = SaWorldEventRepository(db_session_factory)
        rss_source_repository = SaRssSourceRepository(db_session_factory)
        character_event_inbox_repository = SaCharacterEventInboxRepository(
            db_session_factory,
        )
        character_event_mention_repository = SaCharacterEventMentionRepository(
            db_session_factory,
        )
    else:
        from kokoro_link.infrastructure.repositories.in_memory_world_events import (
            InMemoryWorldEventRepository,
        )
        from kokoro_link.infrastructure.repositories.in_memory_rss_sources import (
            InMemoryRssSourceRepository,
        )
        from kokoro_link.infrastructure.repositories.in_memory_character_event_inbox import (
            InMemoryCharacterEventInboxRepository,
        )
        from kokoro_link.infrastructure.repositories.in_memory_character_event_mentions import (
            InMemoryCharacterEventMentionRepository,
        )
        world_event_repository = InMemoryWorldEventRepository()
        rss_source_repository = InMemoryRssSourceRepository()
        character_event_inbox_repository = InMemoryCharacterEventInboxRepository()
        character_event_mention_repository = (
            InMemoryCharacterEventMentionRepository()
        )

    character_relationship_repository: CharacterRelationshipRepositoryPort
    character_peer_profile_repository: CharacterPeerProfileRepositoryPort
    character_encounter_repository: CharacterEncounterRepositoryPort
    character_encounter_intent_repository: CharacterEncounterIntentRepositoryPort
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_character_encounter_repository import (
            SACharacterEncounterRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_character_encounter_intent_repository import (
            SACharacterEncounterIntentRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_character_relationship_repository import (
            SACharacterRelationshipRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_character_peer_profile_repository import (
            SACharacterPeerProfileRepository,
        )

        character_relationship_repository = SACharacterRelationshipRepository(
            db_session_factory,
        )
        character_peer_profile_repository = SACharacterPeerProfileRepository(
            db_session_factory,
        )
        character_encounter_repository = SACharacterEncounterRepository(
            db_session_factory,
        )
        character_encounter_intent_repository = SACharacterEncounterIntentRepository(
            db_session_factory,
        )
    else:
        character_relationship_repository = InMemoryCharacterRelationshipRepository()
        character_peer_profile_repository = InMemoryCharacterPeerProfileRepository()
        character_encounter_repository = InMemoryCharacterEncounterRepository()
        character_encounter_intent_repository = (
            InMemoryCharacterEncounterIntentRepository()
        )

    # Arc template repository — DB-backed with pack rows upserted on
    # startup from the bundled YAML loader. Keeping its own engine /
    # session_factory mirrors the persona / relationship / memoir
    # wiring so the main ``_build_db_repositories`` tuple stays stable.
    arc_template_pack_loader = YAMLArcTemplatePackLoader()
    if db_session_factory is not None:
        arc_template_repository: ArcTemplateRepositoryPort = (
            SAArcTemplateRepository(db_session_factory)
        )
        arc_series_repository: ArcSeriesRepositoryPort = (
            SAArcSeriesRepository(db_session_factory)
        )
    else:
        arc_template_repository = InMemoryArcTemplateRepository()
        arc_series_repository = InMemoryArcSeriesRepository()
    arc_template_pack_sync_service = ArcTemplatePackSyncService(
        loader=arc_template_pack_loader,
        repository=arc_template_repository,
    )
    arc_series_service = ArcSeriesService(
        series_repository=arc_series_repository,
        template_repository=arc_template_repository,
        character_repository=character_repository,
        # GF6 — hosted tone policy (domain/services/story_tone_policy).
        cloud_mode=app_settings.cloud.active,
    )

    operator_profile_service = OperatorProfileService(
        repository=operator_profile_repository,
    )
    # Cross-instance claims (hosted 2×api + 2×worker). ONE lease backend and ONE
    # owner id per process, shared by every per-key claim below, so they all
    # speak the same ownership identity as the rest of the runtime-lease family.
    # Self-host → an in-process lease, i.e. the historical single-process
    # behaviour with no new env to set.
    from kokoro_link.application.services.runtime_claim import RuntimeClaim

    runtime_claim_backend = _build_runtime_lease_backend(
        app_settings, db_session_factory,
    )
    runtime_claim_owner_id = _runtime_lease_owner_id("claim")

    # G2 — hosted player locale / location lifecycle. Cloud mode only; the
    # routes 404 elsewhere, so self-host keeps the repair-CLI-only story and
    # its unchanged route inventory. State lives in ``app_preferences`` under
    # user-scoped keys — no schema change.
    player_locale_service: PlayerLocaleService | None = None
    geocoding_client: GeocodingPort | None = None
    if app_settings.cloud.active:
        player_locale_service = PlayerLocaleService(
            profiles=operator_profile_repository,
            preferences=preferences_repository,
            clock=clock.now,
            # Tells "brand-new player" apart from "account older than the
            # confirmation gate"; the latter is back-filled as confirmed
            # instead of being stranded behind an onboarding dialog.
            characters=character_repository,
            # The rolling guardrail is a read-modify-write over a preference
            # row. Two replicas doing that at once each saw a free slot, so a
            # determined player got 2×N changes and the losing write clobbered
            # the winner's log. The TTL is seconds — the guarded section is
            # three small writes, not an LLM call.
            change_claim=RuntimeClaim(
                runtime_claim_backend,
                prefix=LOCALE_CLAIM_PREFIX,
                owner_id=runtime_claim_owner_id,
                ttl_seconds=LOCALE_CLAIM_TTL_SECONDS,
            ),
        )
        geocoding_client = OpenMeteoGeocodingClient()
    # Per-paid-tier AccountRuntimeProfile comes from the control-plane (plan
    # H2 §5-10) — no hardcoded tier->knob table in Core. Wired only in cloud
    # mode with runtime-config enabled; otherwise paid tiers resolve to the
    # permissive default (today's behavior). The cache never raises, so an
    # outage degrades to the default rather than failing operator requests.
    tier_runtime_profile_port: TierRuntimeProfilePort | None = None
    if app_settings.cloud.active and app_settings.cloud.runtime_config_enabled:
        tier_runtime_profile_port = CachedTierRuntimeProfileResolver(
            client=TierRuntimeProfileClient(
                base_url=app_settings.cloud.user_service_url,
                timeout_seconds=app_settings.cloud.introspect_timeout,
                internal_token=app_settings.cloud.runtime_config_internal_token,
                internal_credential=app_settings.cloud.internal_service_credential,
            ),
        )
    # Character census for the Cloud admin dashboard's load card. Built
    # wherever a database is (the read model is SQL-only by design), and given
    # the SAME tier profile port the due-job cluster resolves dormancy with, so
    # "active" on the card means what "will be reseeded" means in the cluster.
    character_activity_stats_service: CharacterActivityStatsService | None = None
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_character_activity_stats import (
            SACharacterActivityStats,
        )
        character_activity_stats_service = CharacterActivityStatsService(
            stats=SACharacterActivityStats(db_session_factory),
            tier_profiles=tier_runtime_profile_port,
        )
    # U3 — hosted credit ("螢火") balance proxy. Cloud mode + a configured User
    # service only; self-host leaves it None so the route 404s by construction.
    cloud_credit_service: CloudCreditService | None = None
    if app_settings.cloud.active and app_settings.cloud.user_service_url:
        cloud_credit_service = CloudCreditService(
            client=CreditBalanceClient(
                base_url=app_settings.cloud.user_service_url,
                timeout_seconds=app_settings.cloud.introspect_timeout,
                internal_credential=(
                    app_settings.cloud.internal_service_credential
                ),
            ),
        )
    # AP3 — public price list proxy. Same cloud-mode gate as the balance
    # proxy; self-host leaves it None so the route 404s by construction.
    cloud_pricing_service: CloudPricingService | None = None
    cloud_tier_pricing_service: CloudTierPricingService | None = None
    if app_settings.cloud.active and app_settings.cloud.user_service_url:
        cloud_pricing_service = CloudPricingService(
            client=ActionPricingClient(
                base_url=app_settings.cloud.user_service_url,
                timeout_seconds=app_settings.cloud.introspect_timeout,
            ),
        )
        cloud_tier_pricing_service = CloudTierPricingService(
            client=TierActionPricingClient(
                base_url=app_settings.cloud.user_service_url,
                timeout_seconds=app_settings.cloud.introspect_timeout,
                internal_token=app_settings.cloud.runtime_config_internal_token,
                internal_credential=(
                    app_settings.cloud.internal_service_credential
                ),
            ),
        )
    # AN1 — notice-board unread proxy for the in-game dot. Same cloud-mode gate
    # as the balance proxy it sits beside; self-host leaves it None so the route
    # is absent by construction.
    cloud_announcement_service: CloudAnnouncementService | None = None
    if app_settings.cloud.active and app_settings.cloud.user_service_url:
        cloud_announcement_service = CloudAnnouncementService(
            client=AnnouncementUnreadClient(
                base_url=app_settings.cloud.user_service_url,
                timeout_seconds=app_settings.cloud.introspect_timeout,
                internal_credential=(
                    app_settings.cloud.internal_service_credential
                ),
            ),
        )
    account_runtime_profile_resolver = AccountRuntimeProfileResolver(
        operator_profile_repository,
        tier_profile_port=tier_runtime_profile_port,
    )
    # NF4 made the runtime profile a read on EVERY due computation (dormancy
    # covers the un-gated kinds too), and the resolver above sits on an
    # uncached operator-profile row. A short TTL in front of it turns the
    # steady state back into "one read per operator per minute"; a reconcile
    # pass's own per-run memo handles the repeats *within* one pass. Scoped to
    # the due-job cluster on purpose — every other consumer keeps reading
    # through, so no foreground surface starts answering from a memo.
    due_job_profile_resolver = CachedAccountRuntimeProfileResolver(
        account_runtime_profile_resolver,
        ttl_seconds=float(
            _env_int(
                "YURALUME_DUE_PROFILE_TTL",
                int(DEFAULT_PROFILE_CACHE_TTL_SECONDS),
            ),
        ),
    )
    # AP2 — action-level charging. The real service is only built in cloud
    # mode; everywhere else the null object keeps every instrumented entry
    # point on its historical path with no branching at the call sites. Even
    # in cloud mode it charges nothing until a tier's control-plane profile
    # says ``billing_shape=action_fixed``.
    action_billing_service: (
        CloudActionBillingService | NullActionBillingService
    ) = NullActionBillingService()
    if app_settings.cloud.active and app_settings.cloud.user_service_url:
        action_billing_service = CloudActionBillingService(
            client=ActionChargeClient(
                base_url=app_settings.cloud.user_service_url,
                timeout_seconds=app_settings.cloud.introspect_timeout,
                internal_credential=(
                    app_settings.cloud.internal_service_credential
                ),
            ),
            profile_resolver=account_runtime_profile_resolver,
            operator_profiles=operator_profile_repository,
            # The same cache the SPA's price list is served from, so a charge
            # is bound to the number this process actually quoted (C1).
            pricing=cloud_pricing_service,
            # Quote-less server channels resolve the tenant's exact private
            # tier; the public list may intentionally hide that tier.
            tier_pricing=cloud_tier_pricing_service,
        )
    async def _notification_language_resolver(user_id: str) -> str:
        profile = await operator_profile_service.get_for_user(user_id)
        if profile is None:
            return "zh-TW"
        return profile.primary_language or "zh-TW"

    web_push_sender: WebPushSenderPort = (
        PyWebPushSender(
            WebPushVapidConfig(
                public_key=app_settings.web_push.vapid_public_key,
                private_key=app_settings.web_push.vapid_private_key,
                subject=app_settings.web_push.vapid_subject,
                ttl_seconds=app_settings.web_push.ttl_seconds,
            ),
        )
        if app_settings.web_push.configured
        else NullWebPushSender()
    )
    notification_service = NotificationService(
        subscriptions=web_push_subscription_repository,
        preferences=notification_preferences_repository,
        sender=web_push_sender,
        public_base_url=app_settings.public_base_url,
        language_resolver=_notification_language_resolver,
        background=True,
    )
    local_tz = _resolve_local_tz(app_settings.user_timezone)

    # --- Auth (MULTI_USER_AUTH_PLAN Batch 2) -------------------------
    # PasswordHasher: bcrypt for real deployments, fake for fake-provider
    # / unit tests so each test doesn't spend 100ms+ on a hash. Selection
    # mirrors how the chat / image services pick "fake" providers.
    password_hasher: PasswordHasherPort = (
        FakePasswordHasher()
        if app_settings.default_provider_id == _FAKE_PROVIDER_ID
        else BcryptPasswordHasher()
    )

    # JWT: only enforce a real secret when auth is enabled. Disabled
    # mode still gets a service (with a throwaway dev secret) so the
    # /auth/setup route — which works even with auth disabled — can
    # mint a token. The token simply won't be checked anywhere.
    _jwt_secret = app_settings.auth.jwt_secret
    if app_settings.auth.enabled and not _jwt_secret:
        raise RuntimeError(
            "KOKORO_AUTH_ENABLED=true but KOKORO_JWT_SECRET is empty — "
            "set a long random secret in .env or disable auth."
        )
    if not _jwt_secret:
        _jwt_secret = "dev-insecure-jwt-secret-auth-disabled"
    jwt_service = JWTService(
        secret=_jwt_secret,
        ttl_seconds=app_settings.auth.jwt_ttl_seconds,
        absolute_ttl_seconds=app_settings.auth.jwt_absolute_ttl_seconds,
        clock=clock.now,
    )
    # Reads the live holder on each lookup (G0): an Admin GeoIP endpoint /
    # timeout change reaches every process without a restart, and the
    # superseded adapter's HTTP client is released on swap.
    geo_location_provider = ReloadableGeoLocationProvider(
        holder=site_settings_holder,
    )

    auth_service = AuthService(
        repository=operator_profile_repository,
        hasher=password_hasher,
        jwt_service=jwt_service,
        default_timezone_id=app_settings.user_timezone.default_timezone_id,
    )
    cloud_identity_resolver = (
        CloudOperatorIdentityResolver(
            repository=operator_profile_repository,
            subscription_access_guard=subscription_access_guard,
        )
        if app_settings.cloud.active
        else None
    )
    cloud_user_service_client: CloudUserServiceClient | None = None
    if app_settings.cloud.active:
        cloud_user_service_client = CloudUserServiceClient(
            base_url=app_settings.cloud.user_service_url,
            timeout_seconds=app_settings.cloud.introspect_timeout,
            hosted_play_internal_token=app_settings.cloud.hosted_play_internal_token,
            internal_service_credential=app_settings.cloud.internal_service_credential,
        )
        auth_strategy: AuthStrategy = CloudFederatedAuthStrategy(
            user_service=cloud_user_service_client,
            repository=operator_profile_repository,
            jwt_service=jwt_service,
            default_timezone_id=app_settings.user_timezone.default_timezone_id,
            require_paid_tier=app_settings.cloud.require_paid_tier,
        )
    else:
        auth_strategy = LocalAuthStrategy(auth_service)

    prompt_context_builder = DefaultPromptContextBuilder(
        humanization_settings=app_settings.humanization,
        prompt_quality_settings=app_settings.prompt_quality,
        local_tz=local_tz,
        clock=clock,
    )
    state_engine = SimpleStateEngine()
    model_registry = InMemoryChatModelRegistry(default_provider_id=app_settings.default_provider_id)
    model_registry.register(FakeChatModel(provider_id=_FAKE_PROVIDER_ID))
    # Real LLM providers are DB-backed runtime settings. Legacy env provider
    # keys may be seeded into provider_connections during FastAPI startup, but
    # the container no longer registers LLM env directly.

    # Single source of truth for "which LLM does the operator currently
    # want to use" — reads the active-model preference on each call so a
    # mid-session dropdown flip takes effect on memory extraction / goal
    # review / schedule planning / arc planning / consolidation /
    # dialogue summary / prompt rewrite without a process restart.
    _usage_recorder_ref = {"recorder": None}
    cloud_routing_profile_resolver: CloudRoutingProfilePort | None = None
    if app_settings.cloud.active and app_settings.cloud.runtime_config_enabled:
        cloud_routing_profile_resolver = CachedCloudRoutingProfileResolver(
            client=CloudRoutingProfileClient(
                base_url=app_settings.cloud.user_service_url,
                timeout_seconds=app_settings.cloud.introspect_timeout,
                internal_token=app_settings.cloud.runtime_config_internal_token,
                internal_credential=app_settings.cloud.internal_service_credential,
            ),
        )
    # Carries the player's content language down to the adapter chain, which
    # otherwise only ever sees a prompt string. Consumed by the
    # Simplified→Traditional normalisation binding on both providers.
    output_language_resolver = OperatorOutputLanguageResolver(
        repository=operator_profile_repository,
    )
    if app_settings.cloud.active:
        assert cloud_identity_resolver is not None
        active_llm_provider: ActiveLLMProviderPort = CloudActiveLLMProvider(
            identity_resolver=cloud_identity_resolver,
            model_factory=lambda feature_key, identity, default_model: (
                CloudGatewayChatModel(
                    base_url=app_settings.cloud.gateway_url,
                    deployment_token=app_settings.cloud.deployment_token,
                    deployment_id=app_settings.cloud.deployment_id,
                    audience=app_settings.cloud.deployment_audience,
                    default_model=default_model,
                    feature_key=feature_key,
                    identity=identity,
                )
            ),
            model_presets=app_settings.cloud.llm_model_presets,
            account_runtime_profile_resolver=account_runtime_profile_resolver,
            routing_profile_port=cloud_routing_profile_resolver,
            output_language_resolver=output_language_resolver,
        )
    else:
        active_llm_provider = PreferenceBackedActiveLLMProvider(
            registry=model_registry,
            preferences=preferences_repository,
            default_provider_id=app_settings.default_provider_id,
            nsfw_mode_service=nsfw_mode_service,
            output_language_resolver=output_language_resolver,
        )
    active_llm_provider = MeteredActiveLLMProvider(
        inner=active_llm_provider,
        recorder=lambda: _usage_recorder_ref["recorder"],
    )

    # ---- Operator-persona accumulation ---------------------------------
    # Requires a real DB (table ``operator_profile_fields``); skipped on
    # in-memory deployments. ChatService / ProactiveScheduler accept
    # ``None`` and degrade to legacy behaviour (no persona block, no
    # dream tick). The LLM-backed extractor / consolidator only wire up
    # when there's a real provider — fake provider would just pollute
    # staging.
    # ``runtime_claim_backend`` / ``runtime_claim_owner_id`` are built once
    # further up (the locale guard needs them earlier) and shared by every
    # per-key claim in this function.
    operator_persona_service: OperatorPersonaService | None = None
    operator_persona_projection_service: OperatorPersonaProjectionService | None = None
    persona_extraction_service: PersonaExtractionService | None = None
    persona_dream_service: PersonaDreamService | None = None
    persona_curiosity_service: PersonaCuriosityService | None = None
    persona_curiosity_planner: PersonaCuriosityPlannerPort | None = None
    persona_repository = None  # hoisted so the scheduler can inject it
    if app_settings.use_database and app_settings.persona.enabled:
        from kokoro_link.application.services.operator_persona_service import (
            OperatorPersonaService as _OperatorPersonaService,
        )
        from kokoro_link.application.services.persona_curiosity_service import (
            PersonaCuriosityService as _PersonaCuriosityService,
        )
        from kokoro_link.application.services.persona_dream_service import (
            PersonaDreamService as _PersonaDreamService,
        )
        from kokoro_link.application.services.persona_extraction_service import (
            PersonaExtractionService as _PersonaExtractionService,
        )
        from kokoro_link.infrastructure.persistence.sa_operator_persona_repository import (
            SAOperatorPersonaRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_persona_curiosity_repository import (
            SAPersonaCuriosityRepository,
        )
        from kokoro_link.infrastructure.persona.interaction_strength_calculator import (
            InteractionStrengthCalculator,
        )
        from kokoro_link.infrastructure.persona.llm_consolidator import (
            LLMPersonaConsolidator,
        )
        from kokoro_link.infrastructure.persona.llm_extractor import (
            LLMPersonaExtractor,
        )
        from kokoro_link.infrastructure.persona.llm_curiosity_planner import (
            LLMPersonaCuriosityPlanner,
        )
        # Reuse the prefs session factory — persona table lives next to
        # operator_profiles on the same engine. Curiosity attempts share
        # this storage because they are per-character/operator persona
        # metadata, not chat transcripts.
        persona_repository = SAOperatorPersonaRepository(db_session_factory)
        persona_curiosity_repository = SAPersonaCuriosityRepository(
            db_session_factory,
        )
        strength_calculator = InteractionStrengthCalculator(
            session_factory=db_session_factory,
            settings=app_settings.persona,
        )
        operator_persona_service = _OperatorPersonaService(
            repository=persona_repository,
            strength_calculator=strength_calculator,
            settings=app_settings.persona,
        )
        persona_extractor = LLMPersonaExtractor(
            provider=active_llm_provider,
        )
        persona_consolidator = LLMPersonaConsolidator(
            provider=active_llm_provider,
        )
        persona_curiosity_service = _PersonaCuriosityService(
            repository=persona_curiosity_repository,
        )
        if app_settings.persona.curiosity_enabled:
            persona_curiosity_planner = LLMPersonaCuriosityPlanner(
                provider=active_llm_provider,
                feature_key=FEATURE_PERSONA_CURIOSITY,
            )
        persona_extraction_service = _PersonaExtractionService(
            extractor=persona_extractor,
            repository=persona_repository,
            persona_service=operator_persona_service,
        )
        # HUMANIZATION_ROADMAP §3.5 — append a fixed-high salience
        # relationship_milestone memory whenever the Familiarity band
        # crosses a threshold. Pure observation, no LLM call;
        # tail-stage of the dream pass so the band reflects fresh
        # consolidation deltas.
        from kokoro_link.application.services.relationship_milestone_service import (
            RelationshipMilestoneService,
        )
        relationship_milestone_service = RelationshipMilestoneService(
            persona_service=operator_persona_service,
            memory_repository=memory_repository,
            settings=app_settings.humanization,
            operator_profile_service=operator_profile_service,
        )
        persona_dream_service = _PersonaDreamService(
            consolidator=persona_consolidator,
            repository=persona_repository,
            persona_service=operator_persona_service,
            settings=app_settings.persona,
            operator_profile_service=operator_profile_service,
            relationship_milestone_service=relationship_milestone_service,
            clock=clock,
            # Fleet-wide dream cooldown: the service sets the TTL to
            # ``dream_min_interval_hours`` and never releases, so the TTL is the
            # cooldown. Without it the per-process ``_last_run_at`` map lets each
            # worker replica dream the same pair once per window.
            cooldown_claim=RuntimeClaim(
                runtime_claim_backend,
                prefix="dream",
                owner_id=runtime_claim_owner_id,
            ),
        )

    post_turn_processor = _build_post_turn_processor(
        registry=model_registry,
        default_provider_id=app_settings.default_provider_id,
        active_provider=active_llm_provider,
        local_tz=local_tz,
    )
    goal_reviewer = _build_goal_reviewer(
        registry=model_registry,
        default_provider_id=app_settings.default_provider_id,
        active_provider=active_llm_provider,
        local_tz=local_tz,
    )
    schedule_planner = _build_schedule_planner(
        registry=model_registry,
        default_provider_id=app_settings.default_provider_id,
        active_provider=active_llm_provider,
    )
    dialogue_summarizer = _build_dialogue_summarizer(
        registry=model_registry,
        default_provider_id=app_settings.default_provider_id,
        active_provider=active_llm_provider,
    )
    nsfw_safe_summarizer = _build_nsfw_safe_summarizer(
        active_provider=active_llm_provider,
    )

    state_tracker = StateChangeTracker(state_history_repository)
    rest_recovery_refresher = RestRecoveryRefresher(
        character_repository=character_repository,
        state_tracker=state_tracker,
    )
    goal_service = GoalService(goal_repository)
    # P3-Dedup §3.4 — the visible-output slot ledger. DB-backed only (the
    # claim is a DB unique-index race); ``None`` on the in-memory / no-DB path,
    # where the proactive + feed-reply passes fall back to their in-code caps
    # exactly as before. Shared by the dispatcher, the feed-reply service and
    # the daily goal review so their per-slot claims dedup against a
    # distributed reclaimed job. Built here (rather than next to its other
    # consumers further down) because ChatService — constructed well before
    # them — needs the goal-review claim too.
    from kokoro_link.infrastructure.persistence.sa_visible_slots import (
        SAVisibleSlotRepository,
    )

    visible_slot_port = (
        SAVisibleSlotRepository(db_session_factory)
        if db_session_factory is not None else None
    )
    # CF2 — the daily (time-triggered) goal review. Shared by the chat write
    # point (records the day as covered) and the tick / due-job trigger (runs
    # the review). ``visible_slot_port is None`` → the daily trigger is inert
    # and only the chat turn-count cadence reviews, exactly as before.
    daily_goal_review_service = DailyGoalReviewService(
        goal_service=goal_service,
        goal_reviewer=goal_reviewer,
        conversation_repository=conversation_repository,
        visible_slot_port=visible_slot_port,
        operator_profile_service=operator_profile_service,
        local_tz=local_tz,
        clock=clock,
    )
    # StoryArcService must be built before ScheduleService so the latter
    # can read today's scene beat into the planner prompt. Without this
    # the schedule and the arc run on parallel tracks (the schedule
    # planner has no idea today's beat says "在公告欄看試鏡海報" so it
    # makes up "在家看劇" instead). ``arc_template_repository`` was wired
    # earlier alongside ``character_relationship_repository`` so its
    # session_factory can be reused by the pack sync service in lifespan.
    story_arc_planner = _build_story_arc_planner(
        registry=model_registry,
        default_provider_id=app_settings.default_provider_id,
        active_provider=active_llm_provider,
    )
    story_arc_season_decider = _build_story_arc_season_decider(
        active_provider=active_llm_provider,
    )
    story_beat_rechecker = _build_story_beat_rechecker(
        active_provider=active_llm_provider,
    )
    # Arc-template prose translator — LLM-translates a shipped/community
    # template into the operator's primary language at bind/materialise
    # time when the authored language differs (SHIPPED_CONTENT_LOCALIZATION
    # _PLAN Phase 1). Its own feature key routes this short JSON transform
    # to a small/fast model, same as the card translator; fail-soft.
    arc_template_translator = LLMArcTemplateTranslator(
        provider=active_llm_provider,
        feature_key=FEATURE_ARC_TEMPLATE_TRANSLATE,
    )
    # Per-target TTL execution lease (HOSTED_CORE_SCALING §13 Phase 4 前置):
    # shared by fusion + branching execution, startup recovery, the daily
    # story-event roll and lazy story-arc planning so scaled api/worker
    # replicas never double-drive one target. SA lease under the distributed
    # topology, in-process lease (lock-parity) for self-host. Built here —
    # ahead of its first consumer — so the whole process shares ONE owner_id.
    studio_execution_lease = _build_studio_execution_lease(
        app_settings, db_session_factory,
    )
    # Built here rather than next to ``StoryEventService`` below because
    # the arc service is now its second consumer (AE0) and is constructed
    # first — the daily event roll picks up the same instance further
    # down. Stateless apart from its rng, so sharing one is free.
    story_gacha = StoryGachaService(
        seed_repository=story_seed_repository,
        event_repository=story_event_repository,
    )
    story_arc_service = StoryArcService(
        repository=story_arc_repository,
        planner=story_arc_planner,
        local_tz=local_tz,
        conversation_repository=conversation_repository,
        dialogue_summarizer=dialogue_summarizer,
        template_repository=arc_template_repository,
        series_repository=arc_series_repository,
        event_repository=story_event_repository,
        season_decider=story_arc_season_decider,
        beat_rechecker=story_beat_rechecker,
        operator_profile_service=operator_profile_service,
        # AE0 — dramatic-tier seeds reach the arc planner as subject-matter
        # candidates, so a new arc has a source other than the last few
        # chat turns. Fail-soft end to end; an empty pool changes nothing.
        gacha_service=story_gacha,
        template_translator=arc_template_translator,
        # OP1-A — the planner learns who the player is to this character
        # (address terms, relationship, distance) so it can place them in
        # each beat instead of leaving every beat player-less. Fail-soft:
        # no confirmed seed renders the pre-OP1-A prompt.
        relationship_seed_repository=relationship_seed_repository,
        execution_lease=studio_execution_lease,
    )
    # Both follow the live site-settings holder rather than the boot snapshot
    # (G0). Steady state is one frozen-dataclass equality check per call; a
    # changed region / coordinate rebuilds the adapter once, which also drops
    # exactly the cache (holiday calendar / Open-Meteo payloads) that the
    # change invalidated.
    calendar_provider = ReloadableCalendarProvider(
        holder=site_settings_holder, local_tz=local_tz,
    )
    weather_provider = ReloadableWeatherProvider(
        holder=site_settings_holder,
        default_primary_language=app_settings.default_primary_language,
    )
    schedule_service = ScheduleService(
        repository=schedule_repository,
        planner=schedule_planner,
        local_tz=local_tz,
        conversation_repository=conversation_repository,
        dialogue_summarizer=dialogue_summarizer,
        story_arc_service=story_arc_service,
        calendar_context_port=calendar_provider,
        weather_context_port=weather_provider,
        # SE1 — recent story events reach the planner as inspiration facts.
        story_event_repository=story_event_repository,
        relationship_seed_repository=relationship_seed_repository,
        operator_persona_service=operator_persona_service,
        operator_profile_service=operator_profile_service,
        # §4.1 schedule staggering — ON only on the hosted execution backend so
        # a fleet's day+3 generation trickles through the prior day instead of
        # herding at midnight. Self-host (embedded) stays byte-identical: OFF.
        staggering_enabled=(
            app_settings.process.background_backend == "postgres"
        ),
        # Cross-instance single-flight for the full-day plan. The TTL covers one
        # ``plan_day`` (dialogue summary + planner LLM + weather/calendar); the
        # losing replica waits a few seconds for the winner's row rather than
        # blocking a chat request or duplicating the spend.
        #
        # 600s, not the 180s default: this claim has no heartbeat, so the TTL is
        # the entire budget one planner round gets. A round is up to two LLM
        # calls in series (dialogue summary, then ``plan_day``) and the client's
        # read timeout is 300s EACH, so 2×300 is the worst case a call can take
        # without the transport itself giving up. A shorter TTL lapses under a
        # slow model while the winner is still planning, and the replica that
        # picks the day up then pays for a second full round —
        # ``uq_daily_schedules_character_date`` keeps the data right, but the
        # spend doubles and the player can watch the plan flip on refresh.
        # ``ScheduleService`` releases the slot as soon as it finishes, so the
        # generous TTL costs nothing on the happy path.
        plan_claim=RuntimeClaim(
            runtime_claim_backend,
            prefix="sched",
            owner_id=runtime_claim_owner_id,
            ttl_seconds=600,
            wait_seconds=8.0,
        ),
    )
    embedder = _build_embedder(
        settings=app_settings,
        identity_resolver=cloud_identity_resolver,
        routing_profile_port=cloud_routing_profile_resolver,
        provider_credentials_enabled=process_matrix.requires_cloud_provider_credentials,
    )

    memory_consolidator = _build_memory_consolidator(
        registry=model_registry,
        default_provider_id=app_settings.default_provider_id,
        active_provider=active_llm_provider,
    )
    memory_consolidation_service = MemoryConsolidationService(
        memory_repository=memory_repository,
        consolidator=memory_consolidator,
        embedder=embedder,
        character_repository=character_repository,
        operator_profile_service=operator_profile_service,
    )
    memory_admin_service = MemoryAdminService(
        memory_repository=memory_repository,
        embedder=embedder,
    )
    auto_consolidation_trigger: AutoConsolidationTrigger | None = None
    if app_settings.auto_consolidation.enabled:
        auto_consolidation_trigger = AutoConsolidationTrigger(
            memory_repository=memory_repository,
            consolidation_service=memory_consolidation_service,
            # Cooldown + single-flight are claimed on the character row so
            # scaled api replicas can't both consolidate one character.
            character_repository=character_repository,
            threshold=app_settings.auto_consolidation.threshold,
            cooldown=timedelta(
                hours=app_settings.auto_consolidation.cooldown_hours,
            ),
        )
    activity_aftermath_judge = LLMActivityAftermathJudge(
        provider=active_llm_provider,
        feature_key=FEATURE_ACTIVITY_AFTERMATH,
    )
    schedule_memorializer = ScheduleMemorializer(
        schedule_repository=schedule_repository,
        memory_repository=memory_repository,
        local_tz=local_tz,
        embedder=embedder,
        aftermath_port=activity_aftermath_judge,
        character_repository=character_repository,
        operator_profile_service=operator_profile_service,
    )
    # Intra-day weather-drift correction of the planned day. The judge follows
    # the aftermath shape (Null on the fake provider so a self-host demo never
    # pays for a verdict), and the service reads weather + operator through the
    # SAME ports the planner used — comparing the day against a different sky
    # would be worse than not comparing at all.
    schedule_weather_drift_judge = (
        LLMScheduleWeatherDriftJudge(
            provider=active_llm_provider,
            feature_key=FEATURE_SCHEDULE_WEATHER_DRIFT,
        )
        if (
            app_settings.cloud.active
            or app_settings.default_provider_id != _FAKE_PROVIDER_ID
        )
        else NullScheduleWeatherDriftJudge()
    )
    schedule_weather_drift_service = ScheduleWeatherDriftService(
        schedule_repository=schedule_repository,
        drift_port=schedule_weather_drift_judge,
        local_tz=local_tz,
        weather_context_port=weather_provider,
        operator_profile_service=operator_profile_service,
    )
    character_relationship_service = CharacterRelationshipService(
        repository=character_relationship_repository,
        character_repository=character_repository,
    )
    peer_knowledge_consolidator = LLMPeerKnowledgeConsolidator(
        provider=active_llm_provider,
    )
    character_social_knowledge_service = CharacterSocialKnowledgeService(
        peer_profiles=character_peer_profile_repository,
        relationships=character_relationship_repository,
        characters=character_repository,
        memories=memory_repository,
        consolidator=peer_knowledge_consolidator,
        embedder=embedder,
        operator_persona_service=operator_persona_service,
    )
    # Built here (rather than in the chat wiring block below) because the
    # encounter runner shares these adapters; construction has no side
    # effects and all dependencies already exist at this point.
    event_seed_dispenser = EventSeedDispenser(
        inbox_repository=character_event_inbox_repository,
        world_event_repository=world_event_repository,
    )
    register_profiler = (
        LLMRegisterProfiler(
            provider=active_llm_provider,
            feature_key=FEATURE_REGISTER_PROFILE,
        )
        if (
            app_settings.prompt_quality.register_profile_enabled
            and (
                app_settings.cloud.active
                or app_settings.default_provider_id != _FAKE_PROVIDER_ID
            )
        )
        else NullRegisterProfiler()
    )
    novelty_gate = (
        LLMNoveltyGate(
            provider=active_llm_provider,
            feature_key=FEATURE_NOVELTY_GATE,
        )
        if (
            app_settings.prompt_quality.novelty_gate_enabled
            and (
                app_settings.cloud.active
                or app_settings.default_provider_id != _FAKE_PROVIDER_ID
            )
        )
        else NullNoveltyGate()
    )
    encounter_memory_writer = CharacterEncounterMemoryWriter(
        repository=memory_repository,
        embedder=embedder,
    )
    encounter_life_context_builder = CharacterLifeContextBuilder(
        schedule_service=schedule_service,
        goal_repository=goal_repository,
        story_arc_service=story_arc_service,
        event_seed_dispenser=event_seed_dispenser,
        conversation_repository=conversation_repository,
        dialogue_summarizer=dialogue_summarizer,
    )
    character_encounter_planner = CharacterEncounterPlanner(
        relationship_repository=character_relationship_repository,
        encounter_repository=character_encounter_repository,
        character_repository=character_repository,
        schedule_service=schedule_service,
        schedule_repository=schedule_repository,
        provider=active_llm_provider,
        local_tz=local_tz,
        intent_repository=character_encounter_intent_repository,
        operator_profile_service=operator_profile_service,
    )
    character_encounter_runner = CharacterEncounterRunner(
        encounter_repository=character_encounter_repository,
        character_repository=character_repository,
        memory_writer=encounter_memory_writer,
        relationship_service=character_relationship_service,
        provider=active_llm_provider,
        social_knowledge_service=character_social_knowledge_service,
        schedule_service=schedule_service,
        local_tz=local_tz,
        operator_profile_service=operator_profile_service,
        life_context_builder=encounter_life_context_builder,
        register_profiler=register_profiler,
        novelty_gate=novelty_gate,
    )
    character_encounter_service = CharacterEncounterService(
        planner=character_encounter_planner,
        runner=character_encounter_runner,
        encounter_repository=character_encounter_repository,
    )
    feed_reaction_memorializer = FeedReactionMemorializer(
        post_repository=feed_post_repository,
        reaction_repository=feed_reaction_repository,
        comment_repository=feed_comment_repository,
        memory_repository=memory_repository,
        embedder=embedder,
        character_repository=character_repository,
        operator_profile_service=operator_profile_service,
    )
    character_draft_service = CharacterDraftService(
        generator=_build_character_draft_generator(
            active_provider=active_llm_provider,
        ),
        action_billing=action_billing_service,
        # VP4: stage the reference image so a vision model can fetch it by URL
        # instead of receiving it inline; the object is deleted once the draft
        # returns. Both are needed — without a public base URL the stored ref
        # is app-relative and no provider could fetch it.
        object_storage=object_storage,
        public_base_url=app_settings.public_base_url,
    )
    character_personality_type_analyzer = LLMCharacterPersonalityTypeAnalyzer(
        provider=active_llm_provider,
    )
    character_creation_intake_service = CharacterCreationIntakeService(
        provider=active_llm_provider,
        personality_type_analyzer=character_personality_type_analyzer,
    )
    companion_draft_service = CompanionDraftService(
        generator=_build_companion_draft_generator(
            active_provider=active_llm_provider,
        ),
        characters=character_repository,
    )
    image_profile_registry = _build_image_profile_registry(
        settings=app_settings, registry=model_registry,
        active_provider=active_llm_provider,
    )
    if app_settings.cloud.active:
        assert cloud_identity_resolver is not None
        active_image_provider = CloudActiveImageProvider(
            provider_factory=lambda feature_key, preset: CloudGatewayImageProvider(
                base_url=app_settings.cloud.gateway_url,
                deployment_token=app_settings.cloud.deployment_token,
                deployment_id=app_settings.cloud.deployment_id,
                audience=app_settings.cloud.deployment_audience,
                preset=preset,
                feature_key=feature_key,
                identity_resolver=cloud_identity_resolver,
                # EC6: lets the adapter turn a managed character's locally
                # stored reference art into real image input on the request.
                object_storage=object_storage,
            ),
            identity_resolver=cloud_identity_resolver,
            routing_profile_port=cloud_routing_profile_resolver,
            default_preset=app_settings.cloud.image_preset,
        )
    else:
        active_image_provider = PreferenceBackedActiveImageProvider(
            registry=image_profile_registry,
            preferences=preferences_repository,
            nsfw_mode_service=nsfw_mode_service,
        )
    video_profile_registry = _build_video_profile_registry(
        settings=app_settings,
    )
    # CV4: the asynchronous job path is opt-in per deployment, because it
    # needs a broker on the other side of the Gateway that the sync
    # ``/v1/videos/generations`` route does not. Off → the adapter declares
    # no async capability, the composer resolves to it and takes its
    # synchronous branch, and no pending row is ever written. Enabling it is
    # what turns LumeGram video into the deferred pipeline.
    video_jobs_enabled = _env_flag("KOKORO_VIDEO_JOBS_ENABLED", False)
    if app_settings.cloud.active:
        assert cloud_identity_resolver is not None
        active_video_provider = CloudActiveVideoProvider(
            provider_factory=lambda feature_key, preset: CloudGatewayVideoProvider(
                base_url=app_settings.cloud.gateway_url,
                deployment_token=app_settings.cloud.deployment_token,
                deployment_id=app_settings.cloud.deployment_id,
                audience=app_settings.cloud.deployment_audience,
                preset=preset,
                feature_key=feature_key,
                identity_resolver=cloud_identity_resolver,
                jobs_enabled=video_jobs_enabled,
            ),
            identity_resolver=cloud_identity_resolver,
            routing_profile_port=cloud_routing_profile_resolver,
            default_preset=app_settings.cloud.video_preset,
        )
    else:
        active_video_provider = PreferenceBackedActiveVideoProvider(
            registry=video_profile_registry,
            preferences=preferences_repository,
        )
    visual_generation_style_service = VisualGenerationStyleService(
        preferences=preferences_repository,
    )
    # The album gacha's pending batch must be visible to every api replica:
    # generate and commit are separate requests and hosted load-balances
    # several processes, so a process-local batch drops the player's picks.
    from kokoro_link.contracts.character_image_candidate_batch import (
        CharacterImageCandidateBatchRepositoryPort,
    )
    character_image_candidate_batch_repository: (
        CharacterImageCandidateBatchRepositoryPort
    )
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_character_image_candidate_batch_repository import (  # noqa: E501
            SACharacterImageCandidateBatchRepository,
        )
        character_image_candidate_batch_repository = (
            SACharacterImageCandidateBatchRepository(db_session_factory)
        )
    else:
        from kokoro_link.infrastructure.repositories.in_memory_character_image_candidate_batches import (  # noqa: E501
            InMemoryCharacterImageCandidateBatchRepository,
        )
        character_image_candidate_batch_repository = (
            InMemoryCharacterImageCandidateBatchRepository()
        )
    character_image_service = CharacterImageService(
        character_repository=character_repository,
        uploads_dir=app_settings.uploads_dir,
        object_storage=object_storage,
        image_provider=active_image_provider,
        visual_style_service=visual_generation_style_service,
        account_runtime_profile_resolver=account_runtime_profile_resolver,
        subscription_access_guard=subscription_access_guard,
        candidate_batch_repository=character_image_candidate_batch_repository,
        action_billing=action_billing_service,
    )
    character_lora_service = CharacterLoraService(
        character_repository=character_repository,
        lora_dir=app_settings.comfyui.lora_dir,
    )
    album_service = AlbumService(
        album_repository=album_repository,
        character_repository=character_repository,
        uploads_dir=app_settings.uploads_dir,
        object_storage=object_storage,
    )

    tool_registry = _build_tool_registry(
        settings=app_settings, image_provider=active_image_provider,
        object_storage=object_storage,
        album_service=album_service,
        visual_style_service=visual_generation_style_service,
        action_billing=action_billing_service,
        cloud_identity_resolver=cloud_identity_resolver,
        cloud_routing_profile_resolver=cloud_routing_profile_resolver,
        provider_credentials_enabled=process_matrix.requires_cloud_provider_credentials,
    )
    tool_orchestrator = ToolOrchestrator(
        registry=tool_registry,
        invocation_repository=tool_invocation_repository,
    )

    # story_gacha, arc_template_repository, story_arc_planner and
    # story_arc_service are built earlier (before schedule_service) so the
    # schedule planner can read scene beats and the arc planner can draw
    # dramatic seed candidates. The single shared YAML repo cache covers
    # all callers (story arc service + REST list endpoint + wizard).
    story_expander = _build_story_expander(
        registry=model_registry,
        default_provider_id=app_settings.default_provider_id,
        active_provider=active_llm_provider,
        # GF6 — hosted tone policy (domain/services/story_tone_policy).
        cloud_mode=app_settings.cloud.active,
    )
    # Phase 2.7 — wizard backend. Stateless service, single shared
    # instance is fine. Routes through the per-feature LLM resolver
    # (FEATURE_ARC_TEMPLATE_INTAKE) so operators can pin a different
    # provider for wizard work than for runtime chat.
    arc_template_intake_service = ArcTemplateIntakeService(
        repository=arc_template_repository,
        provider=active_llm_provider,
        cloud_mode=app_settings.cloud.active,
    )
    arc_completion_memory_writer = _build_arc_completion_memory_writer(
        active_provider=active_llm_provider,
    )
    story_event_service = StoryEventService(
        gacha=story_gacha,
        expander=story_expander,
        event_repository=story_event_repository,
        memory_repository=memory_repository,
        embedder=embedder,
        local_tz=local_tz,
        arc_service=story_arc_service,
        arc_completion_memory_writer=arc_completion_memory_writer,
        operator_profile_service=operator_profile_service,
        execution_lease=studio_execution_lease,
    )
    story_beat_scene_service = StoryBeatSceneService(
        story_arc_service=story_arc_service,
        story_event_service=story_event_service,
        writer=LLMStoryBeatSceneWriter(
            provider=active_llm_provider,
            feature_key=FEATURE_ARC_SCENE_WRITE,
            cloud_mode=app_settings.cloud.active,
        ),
        local_tz=local_tz,
        operator_profile_service=operator_profile_service,
    )

    # 起幕 (SC1) — the player-pulled scene runtime. The session row is the
    # cross-replica state: the api replica that opens a scene is routinely
    # not the one that serves its next turn or closes it on timeout, so it
    # must never be a process attribute.
    story_scene_session_repository: StorySceneSessionRepositoryPort
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_story_scene_session_repository import (
            SAStorySceneSessionRepository,
        )
        story_scene_session_repository = SAStorySceneSessionRepository(
            db_session_factory,
        )
    else:
        story_scene_session_repository = InMemoryStorySceneSessionRepository()
    # SC1-C: the in-scene action chips. One writer instance serves both
    # call sites — the opening (below) and every in-scene chat turn
    # (``ChatService``) — so the two surfaces cannot drift on how a chip
    # is written or which feature key routes it.
    story_scene_chips_writer = LLMStorySceneChipsWriter(
        provider=active_llm_provider,
        feature_key=FEATURE_STORY_SCENE_CHIPS,
    )
    # Player-declared identity / world premise — read by every composer
    # that speaks to the player (chat, proactive, busy-defer release,
    # 起幕, LumeGram reply, 發話輔助) and by the post-turn extractor as a
    # do-not-re-extract list; written by the per-character PUT.
    #
    # Built here, ahead of the first consumer, rather than beside the
    # other per-pair repositories further down: the story-scene service is
    # wired before that point and a repository that arrives late is a
    # dependency that silently stays ``None``.
    from kokoro_link.infrastructure.repositories.in_memory_player_persona_notes import (
        InMemoryPlayerPersonaNoteRepository,
    )
    player_persona_note_repository: PlayerPersonaNoteRepositoryPort
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_player_persona_note_repository import (
            SAPlayerPersonaNoteRepository,
        )
        player_persona_note_repository = SAPlayerPersonaNoteRepository(
            db_session_factory,
        )
    else:
        player_persona_note_repository = InMemoryPlayerPersonaNoteRepository()
    player_persona_note_service = PlayerPersonaNoteService(
        player_persona_note_repository,
    )
    # NF4: the foreground-interaction anchor (``CharacterState.last_active_at``)
    # for the paid foreground surfaces that are not chat — chat writes it as
    # part of its own turn save, everything else needs this targeted, monotonic
    # single-column touch. One instance, shared: it holds no state.
    #
    # **Cloud mode only** (plan §5 "self-host 行為零變化"). Dormancy is the
    # reason this exists and it can only ever fire under a control-plane knob
    # self-host never receives — but ``last_active_at`` is NOT a dormancy-only
    # field: on self-host it is also read by ``_has_user_started_interaction``
    # (NULL ⇒ "the player never opened their mouth" ⇒ no proactive messages at
    # all), ``_compute_idle_minutes``, the feed's silence anchor and the
    # runtime activity gate. Advancing it from 分歧劇場／起幕／融合故事 would
    # therefore make a self-host player who only plays those surfaces start
    # receiving proactive messages they never used to get — a behaviour change
    # with the knob still NULL. So self-host gets ``None`` (the three services
    # treat it as "don't track" and keep their pre-NF4 path byte for byte) and
    # the anchor stays what its docstring claims: neutral BY CONSTRUCTION.
    character_activity_anchor: CharacterActivityAnchor | None = None
    if app_settings.cloud.active:
        character_activity_anchor = CharacterActivityAnchor(
            character_repository, clock=clock,
        )

    story_scene_service = StorySceneService(
        sessions=story_scene_session_repository,
        conversations=conversation_repository,
        opener=LLMStorySceneOpener(
            provider=active_llm_provider,
            feature_key=FEATURE_STORY_SCENE_OPEN,
            cloud_mode=app_settings.cloud.active,
        ),
        chips_writer=story_scene_chips_writer,
        # §3.1 waterfall, in order: play the next beat of a running season,
        # else force-open the next season and play its first beat, else an
        # ad-hoc side story. Tuple order IS the waterfall order.
        material_providers=(
            PendingBeatSceneMaterialProvider(
                story_arc_service=story_arc_service,
            ),
            ForcedSeasonSceneMaterialProvider(
                story_arc_service=story_arc_service,
            ),
            SideStorySceneMaterialProvider(
                memory_repository=memory_repository,
                relationship_seed_repository=relationship_seed_repository,
                conversation_repository=conversation_repository,
                dialogue_summarizer=dialogue_summarizer,
                # Dramatic-tier seeds are this layer's designated material
                # source. An empty pool is
                # the expected state until SE3 is imported, and degrades to
                # relationship + memory rather than failing the scene.
                gacha_service=story_gacha,
                operator_profile_service=operator_profile_service,
            ),
        ),
        story_arc_service=story_arc_service,
        # Shares the chat turn's per-conversation mutex: an opening writes
        # two messages into the same thread a chat turn writes into, and
        # the two must not interleave.
        turn_lease=ChatTurnLease.from_studio_lease(studio_execution_lease),
        operator_profile_service=operator_profile_service,
        # SC3-B: per-tier daily ceiling on openings. The knob defaults to
        # unlimited (None) so self-host never sees the guard fire.
        quota_guard=StorySceneQuotaGuard(
            sessions=story_scene_session_repository,
            characters=character_repository,
            profiles=account_runtime_profile_resolver,
        ),
        local_tz=local_tz,
        # SC1-D: the shared wrap-up behind all three close routes (the
        # in-turn verdict, 「結束場景」, and SC1-E's idle sweep). The canon
        # collaborators are the *existing* ones — a realized beat has to
        # land through the same chain a scheduled beat does, or a
        # player-pulled scene would quietly write different history.
        closer=LLMStorySceneCloser(
            provider=active_llm_provider,
            feature_key=FEATURE_STORY_SCENE_CLOSE,
        ),
        story_event_service=story_event_service,
        memory_repository=memory_repository,
        embedder=embedder,
        # SC3-C: 起幕 is an action-priced entry point in cloud mode. Same
        # null object as every other instrumented service, so self-host and
        # token-billed tiers keep the exact path they had before.
        action_billing=action_billing_service,
        player_persona_note_repository=player_persona_note_repository,
        activity_anchor=character_activity_anchor,
    )

    self_repetition_extractor = LLMSelfRepetitionExtractor(
        provider=active_llm_provider,
        feature_key=FEATURE_CHAT_REPETITION_CHECK,
    )

    idle_drift_judge = LLMIdleDriftJudge(
        provider=active_llm_provider,
        feature_key=FEATURE_IDLE_DRIFT,
    )

    busy_reply_decider = LLMBusyReplyDecider(
        provider=active_llm_provider,
        feature_key=FEATURE_BUSY_REPLY_DECIDE,
        local_tz=local_tz,
    )
    pending_follow_up_composer = LLMPendingFollowUpComposer(
        provider=active_llm_provider,
        feature_key=FEATURE_BUSY_FOLLOW_UP,
    )
    scheduled_promise_composer = LLMScheduledPromiseComposer(
        provider=active_llm_provider,
        feature_key=FEATURE_SCHEDULED_PROMISE,
    )

    # Turn recorder — captures prompt / response / latency / refs for
    # every LLM turn (chat + post-turn + proactive). Read side lives in
    # the observability dashboard + replay CLI. Repo lives outside the
    # main ``_RepoBundle`` for the same reason as fusion_story etc.
    from kokoro_link.contracts.observability import (
        TurnRecorderPort,
        TurnRecordRepositoryPort,
    )
    from kokoro_link.contracts.generation_usage import (
        UsageEventRecorderPort,
        UsageEventRepositoryPort,
    )
    from kokoro_link.contracts.account_runtime_usage import (
        AccountRuntimeUsageRepositoryPort,
    )
    from kokoro_link.contracts.emotion import EmotionEventRepositoryPort
    from kokoro_link.infrastructure.observability.turn_recorder import (
        BackgroundTurnRecorder,
    )
    from kokoro_link.infrastructure.usage.recorder import (
        BackgroundUsageEventRecorder,
    )
    from kokoro_link.infrastructure.usage.price_estimator import (
        StaticPriceEstimator,
    )
    turn_record_repository: TurnRecordRepositoryPort
    usage_event_repository: UsageEventRepositoryPort
    account_runtime_usage_repository: AccountRuntimeUsageRepositoryPort
    emotion_event_repository: EmotionEventRepositoryPort
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_turn_record_repository import (
            SATurnRecordRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_generation_usage_repository import (
            SAGenerationUsageRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_account_runtime_usage_repository import (
            SAAccountRuntimeUsageRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_emotion_event_repository import (
            SAEmotionEventRepository,
        )
        turn_record_repository = SATurnRecordRepository(db_session_factory)
        usage_event_repository = SAGenerationUsageRepository(db_session_factory)
        account_runtime_usage_repository = SAAccountRuntimeUsageRepository(
            db_session_factory,
        )
        emotion_event_repository = SAEmotionEventRepository(db_session_factory)
        from kokoro_link.infrastructure.persistence.sa_deferred_intent_repository import (
            SADeferredIntentRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_behavioral_pattern_repository import (
            SABehavioralPatternRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_self_reflection_repository import (
            SASelfReflectionRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_disposition_drift_repository import (
            SADispositionDriftHistoryRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_memoir_pin_repository import (
            SAMemoirPinRepository,
        )
        deferred_intent_repository = SADeferredIntentRepository(db_session_factory)
        behavioral_pattern_repository = SABehavioralPatternRepository(
            db_session_factory,
        )
        self_reflection_repository = SASelfReflectionRepository(
            db_session_factory,
        )
        disposition_drift_history_repository = SADispositionDriftHistoryRepository(
            db_session_factory,
        )
        memoir_pin_repository: MemoirPinRepositoryPort = SAMemoirPinRepository(
            db_session_factory,
        )
    else:
        from kokoro_link.infrastructure.repositories.in_memory_turn_records import (
            InMemoryTurnRecordRepository,
        )
        from kokoro_link.infrastructure.repositories.in_memory_generation_usage import (
            InMemoryGenerationUsageRepository,
        )
        from kokoro_link.infrastructure.repositories.in_memory_account_runtime_usage import (
            InMemoryAccountRuntimeUsageRepository,
        )
        from kokoro_link.infrastructure.repositories.in_memory_emotion_events import (
            InMemoryEmotionEventRepository,
        )
        from kokoro_link.infrastructure.repositories.in_memory_deferred_intents import (
            InMemoryDeferredIntentRepository,
        )
        from kokoro_link.infrastructure.repositories.in_memory_behavioral_patterns import (
            InMemoryBehavioralPatternRepository,
        )
        from kokoro_link.infrastructure.repositories.in_memory_self_reflections import (
            InMemorySelfReflectionRepository,
        )
        from kokoro_link.infrastructure.repositories.in_memory_disposition_drift import (
            InMemoryDispositionDriftHistoryRepository,
        )
        from kokoro_link.infrastructure.repositories.in_memory_memoir_pins import (
            InMemoryMemoirPinRepository,
        )
        turn_record_repository = InMemoryTurnRecordRepository()
        usage_event_repository = InMemoryGenerationUsageRepository()
        account_runtime_usage_repository = InMemoryAccountRuntimeUsageRepository()
        emotion_event_repository = InMemoryEmotionEventRepository()
        deferred_intent_repository = InMemoryDeferredIntentRepository()
        behavioral_pattern_repository = InMemoryBehavioralPatternRepository()
        self_reflection_repository = InMemorySelfReflectionRepository()
        disposition_drift_history_repository = (
            InMemoryDispositionDriftHistoryRepository()
        )
        memoir_pin_repository = InMemoryMemoirPinRepository()
    # AP4 — the paid escape hatch from the two tier allowances that are hard
    # walls today. Always built (the switches must be readable and clearable
    # even on a tier that closed overage); it grants nothing unless the tier
    # opened overage AND the player turned that item on, so self-host and
    # every un-migrated tier keep the walls exactly where they were.
    quota_overage_service = QuotaOverageService(
        preferences=preferences_repository,
        profile_resolver=account_runtime_profile_resolver,
        usage_repository=account_runtime_usage_repository,
        action_billing=action_billing_service,
        # Consent is to a price, so the switch surface and the spend check both
        # read the same published list the player is quoted from. ``None`` on
        # self-host, where the switches are not mounted at all.
        pricing=cloud_pricing_service,
    )
    # Read-only mirror of the same ceilings, for the "you have 2 of 3 slots
    # left" hints. Built here because this is the first point at which both
    # counters it needs exist (the usage ledger just above, the scene session
    # repository further up); it writes nothing and gates nothing, so its
    # position in the wiring order carries no other constraint.
    player_runtime_limits_service = PlayerRuntimeLimitsService(
        profiles=account_runtime_profile_resolver,
        characters=character_repository,
        usage_repository=account_runtime_usage_repository,
        story_scene_sessions=story_scene_session_repository,
        clock=clock,
    )
    turn_recorder: TurnRecorderPort = BackgroundTurnRecorder(turn_record_repository)
    usage_price_estimator = StaticPriceEstimator.from_json_file(
        os.environ.get("KOKORO_USAGE_PRICE_CATALOG_PATH"),
    )
    usage_recorder: UsageEventRecorderPort = BackgroundUsageEventRecorder(
        usage_event_repository,
        price_estimator=usage_price_estimator,
    )
    _usage_recorder_ref["recorder"] = usage_recorder
    character_image_service.set_usage_recorder(usage_recorder)
    for tool in tool_registry.all():
        setter = getattr(tool, "set_usage_recorder", None)
        if callable(setter):
            setter(usage_recorder)
    rest_recovery_refresher.set_emotion_event_repository(
        emotion_event_repository,
    )
    # HUMANIZATION_ROADMAP §3.4 — wire the deferred-intent service so the
    # proactive dispatcher can park motives blocked by the intention judge
    # and re-surface them on subsequent ticks.
    from kokoro_link.application.services.deferred_intent_service import (
        DeferredIntentService,
    )
    deferred_intent_service = DeferredIntentService(
        repository=deferred_intent_repository,
        settings=app_settings.humanization,
    )
    # HUMANIZATION_ROADMAP §3.3 — behavioural pattern observer. Schedule
    # statistics always available; phrase-habit LLM only when a real
    # provider is wired (the fake provider would just hallucinate
    # imaginary 口頭禪).
    from kokoro_link.application.services.behavioral_pattern_service import (
        BehavioralPatternObserverService,
    )
    from kokoro_link.infrastructure.behavior.llm_phrase_habit_extractor import (
        LLMPhraseHabitExtractor,
    )
    phrase_habit_extractor = LLMPhraseHabitExtractor(
        provider=active_llm_provider,
    )
    behavioral_pattern_service = BehavioralPatternObserverService(
        repository=behavioral_pattern_repository,
        schedule_repository=schedule_repository,
        conversation_repository=conversation_repository,
        phrase_habit_extractor=phrase_habit_extractor,
        settings=app_settings.humanization,
        local_tz=local_tz,
    )
    # Late-bind on the persona dream service so the tail stage of the
    # dream pass picks behavioural patterns up. The dream service was
    # built earlier (line ~1430) before behavioural_pattern_service
    # existed; see ``set_behavioral_pattern_service`` for the rationale.
    if persona_dream_service is not None:
        persona_dream_service.set_behavioral_pattern_service(
            behavioral_pattern_service,
        )
        persona_dream_service.set_character_repository(character_repository)
    # Late-bind on the schedule service so the planner sees recurring
    # patterns the dream pass has written. Setter pattern for the same
    # ordering reason — schedule_service is built before the observability
    # engine's repos exist.
    schedule_service.set_behavioral_pattern_repository(
        behavioral_pattern_repository,
    )

    # HUMANIZATION_ROADMAP §3.2 — self-reflection dream-time pipeline.
    # Wire only when a real provider is available; the fake provider
    # would write hallucinated narratives.
    from kokoro_link.application.services.self_reflection_service import (
        SelfReflectionService,
    )
    from kokoro_link.contracts.self_reflection import (
        NullSelfReflectionGenerator,
    )
    from kokoro_link.infrastructure.reflection.llm_generator import (
        LLMSelfReflectionGenerator,
    )
    reflection_generator = LLMSelfReflectionGenerator(
        provider=active_llm_provider,
    )
    self_reflection_service = SelfReflectionService(
        repository=self_reflection_repository,
        memory_repository=memory_repository,
        emotion_event_repository=emotion_event_repository,
        generator=reflection_generator,
        settings=app_settings.humanization,
        operator_profile_service=operator_profile_service,
        clock=clock,
    )
    if persona_dream_service is not None:
        persona_dream_service.set_self_reflection_service(
            self_reflection_service,
        )

    # Player-side memoir view + pin store. Reuses
    # the existing reader ports (memory / reflection / emotion) and adds
    # one new pin repository. The optional localizer only translates
    # existing player-visible prose; it does not generate new memoir
    # content.
    from kokoro_link.application.services.memoir_service import (
        MemoirService,
    )
    memoir_service = MemoirService(
        memory_repository=memory_repository,
        self_reflection_repository=self_reflection_repository,
        emotion_event_repository=emotion_event_repository,
        pin_repository=memoir_pin_repository,
        settings=app_settings.memoir,
        localizer=LLMMemoirLocalizer(
            provider=active_llm_provider,
            feature_key=FEATURE_MEMOIR_LOCALIZE,
        ),
        operator_profile_service=operator_profile_service,
    )

    # HUMANIZATION_ROADMAP §3.1 — disposition drift judge + service.
    from kokoro_link.application.services.disposition_drift_service import (
        DispositionDriftService,
    )
    from kokoro_link.contracts.disposition_drift import (
        NullDispositionDriftJudge,
    )
    from kokoro_link.infrastructure.disposition.llm_drift_judge import (
        LLMDispositionDriftJudge,
    )
    disposition_drift_judge = LLMDispositionDriftJudge(
        provider=active_llm_provider,
    )
    disposition_drift_service = DispositionDriftService(
        character_repository=character_repository,
        history_repository=disposition_drift_history_repository,
        memory_repository=memory_repository,
        emotion_event_repository=emotion_event_repository,
        judge=disposition_drift_judge,
        settings=app_settings.humanization,
        clock=clock,
    )
    if persona_dream_service is not None:
        persona_dream_service.set_disposition_drift_service(
            disposition_drift_service,
        )

    # HUMANIZATION_ROADMAP §4.5 — quiet hours window.
    # Switched to ``app_preferences`` (via scoped_preferences) in
    # 2026-05-26 multi-user phase 2 so each user can keep their own
    # window — the legacy ``app_runtime_settings`` rows stay on disk
    # but are no longer the runtime source. Env defaults still apply
    # when neither a user override nor a global preference exists.
    from kokoro_link.application.services.quiet_hours_service import (
        QuietHoursService,
    )
    from kokoro_link.infrastructure.repositories.in_memory_runtime_settings import (
        InMemoryRuntimeSettingsRepository,
    )
    runtime_settings_repository = None
    # Single shared factory (or None on the in-memory path) drives every
    # remaining SA repo below — turn-record, provider-settings, address,
    # experiment. Direct ref replaces the old ``locals().get(...)`` probes.
    _tr_factory_for_settings = db_session_factory
    if _tr_factory_for_settings is not None:
        from kokoro_link.infrastructure.persistence.sa_runtime_settings_repository import (
            SARuntimeSettingsRepository,
        )
        runtime_settings_repository = SARuntimeSettingsRepository(
            _tr_factory_for_settings,
        )
    else:
        runtime_settings_repository = InMemoryRuntimeSettingsRepository()
    # G0: built here because this is the first point where the KV repository and
    # the holder are both in scope. Shared verbatim by the admin write path
    # (local convergence) and the cross-process refresher (remote convergence).
    from kokoro_link.bootstrap.site_settings_refresh import (
        build_site_settings_reloader,
    )
    site_settings_reloader = build_site_settings_reloader(
        holder=site_settings_holder,
        repository=runtime_settings_repository,
        env_settings=env_app_settings,
    )
    quiet_hours_service = QuietHoursService(
        preferences=preferences_repository,
        env_start=app_settings.persona.dream_quiet_hours_start,
        env_end=app_settings.persona.dream_quiet_hours_end,
        clock=clock,
    )
    if persona_dream_service is not None:
        persona_dream_service.set_quiet_hours_service(quiet_hours_service)

    # BYOK provider settings — encrypted installation-level provider
    # connections managed from Admin UI. The repository is separate
    # from generic runtime settings because it carries secrets.
    provider_connection_repository: ProviderConnectionRepositoryPort
    _provider_settings_factory = db_session_factory
    if _provider_settings_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_provider_connection_repository import (
            SAProviderConnectionRepository,
        )

        provider_connection_repository = SAProviderConnectionRepository(
            _provider_settings_factory,
        )
    else:
        from kokoro_link.infrastructure.repositories.in_memory_provider_connections import (
            InMemoryProviderConnectionRepository,
        )

        provider_connection_repository = InMemoryProviderConnectionRepository()
    provider_connection_service = ProviderConnectionService(
        repository=provider_connection_repository,
        cipher=ProviderSecretCipher(app_settings.config_encryption_key),
    )

    # HUMANIZATION_ROADMAP §4.2 — address preference observer.
    from kokoro_link.infrastructure.repositories.in_memory_address_preferences import (
        InMemoryOperatorAddressPreferenceRepository,
    )
    from kokoro_link.infrastructure.behavior.llm_address_observer import (
        LLMAddressObserver,
        NullAddressObserver,
    )
    address_preference_repository: OperatorAddressPreferenceRepositoryPort
    if _tr_factory_for_settings is not None:
        from kokoro_link.infrastructure.persistence.sa_address_preference_repository import (
            SAOperatorAddressPreferenceRepository,
        )
        address_preference_repository = SAOperatorAddressPreferenceRepository(
            _tr_factory_for_settings,
        )
    else:
        address_preference_repository = (
            InMemoryOperatorAddressPreferenceRepository()
        )
    # Per-pair address-change (rename) log — read by the chat prompt to
    # surface the latest rename, written by the relationship-names PATCH.
    from kokoro_link.infrastructure.repositories.in_memory_address_change_log import (
        InMemoryAddressChangeLogRepository,
    )
    address_change_log_repository: AddressChangeLogRepositoryPort
    if _tr_factory_for_settings is not None:
        from kokoro_link.infrastructure.persistence.sa_address_change_log_repository import (
            SAAddressChangeLogRepository,
        )
        address_change_log_repository = SAAddressChangeLogRepository(
            _tr_factory_for_settings,
        )
    else:
        address_change_log_repository = InMemoryAddressChangeLogRepository()
    # Player-facing per-pair address-name edit (seed + rename-log + persona
    # reconcile). Persona service may be None in trimmed builds; the service
    # degrades to seed + rename-log only.
    relationship_names_service = RelationshipNamesService(
        seed_repository=relationship_seed_repository,
        change_log_repository=address_change_log_repository,
        persona_service=operator_persona_service,
    )
    if app_settings.humanization.address_preference_enabled:
        _address_observer = LLMAddressObserver(
            provider=active_llm_provider,
            feature_key=FEATURE_ADDRESS_PREFERENCE_OBSERVER,
        )
    else:
        _address_observer = NullAddressObserver()
    address_preference_service = AddressPreferenceObserverService(
        repository=address_preference_repository,
        observer=_address_observer,
        settings=app_settings.humanization,
        conversation_repository=conversation_repository,
        # #3 direction-inversion guard: drop an observed salutation that
        # collides with the seed user-address-name or the operator's own
        # display name (a suspected direction flip). Fail-soft when either
        # dep is missing.
        seed_repository=relationship_seed_repository,
        operator_profile_service=operator_profile_service,
    )
    if persona_dream_service is not None:
        persona_dream_service.set_address_preference_service(
            address_preference_service,
        )

    # Relationship coherence self-heal (dream tail). Uses a high-reasoning
    # detector (own feature key) so owners can pin a stronger model, or a
    # null detector when the feature is disabled / the provider is fake.
    from kokoro_link.application.services.relationship_coherence_service import (
        RelationshipCoherenceService,
    )
    from kokoro_link.infrastructure.persona.llm_relationship_coherence_detector import (
        LLMRelationshipCoherenceDetector,
        NullRelationshipCoherenceDetector,
    )
    if app_settings.humanization.relationship_coherence_enabled:
        _coherence_detector = LLMRelationshipCoherenceDetector(
            provider=active_llm_provider,
            feature_key=FEATURE_RELATIONSHIP_COHERENCE,
        )
    else:
        _coherence_detector = NullRelationshipCoherenceDetector()
    relationship_coherence_service = RelationshipCoherenceService(
        detector=_coherence_detector,
        persona_service=operator_persona_service,
        seed_repository=relationship_seed_repository,
        change_log_repository=address_change_log_repository,
        character_repository=character_repository,
        operator_profile_service=operator_profile_service,
        address_preference_repository=address_preference_repository,
        memory_repository=memory_repository,
        conversation_repository=conversation_repository,
        transcript_window=(
            app_settings.humanization.relationship_coherence_transcript_window
        ),
    )
    if (
        persona_dream_service is not None
        and operator_persona_service is not None
    ):
        persona_dream_service.set_relationship_coherence_service(
            relationship_coherence_service,
        )

    # HUMANIZATION_ROADMAP §4.5 — LLM serialisation gate. Wired here so
    # any caller (dream service, embedding sync, proactive dispatcher)
    # can pull it through container DI without re-instantiating.
    llm_priority_gate = LLMSerialisationGate(concurrency=1)
    if persona_dream_service is not None:
        persona_dream_service.set_priority_gate(llm_priority_gate)

    # HUMANIZATION_ROADMAP §4.6 — A/B experiment framework. Persistent SA
    # repos when the observability engine is wired; falls back to
    # in-memory for fake-provider / unit-test runs.
    from kokoro_link.infrastructure.repositories.in_memory_experiments import (
        InMemoryExperimentAssignmentRepository,
        InMemoryExperimentRepository,
    )
    if _tr_factory_for_settings is not None:
        from kokoro_link.infrastructure.persistence.sa_experiment_repository import (
            SAExperimentAssignmentRepository,
            SAExperimentRepository,
        )
        experiment_repository = SAExperimentRepository(_tr_factory_for_settings)
        experiment_assignment_repository = SAExperimentAssignmentRepository(
            _tr_factory_for_settings,
        )
    else:
        experiment_repository = InMemoryExperimentRepository()
        experiment_assignment_repository = InMemoryExperimentAssignmentRepository()
    experiment_service = ExperimentService(
        experiment_repository=experiment_repository,
        assignment_repository=experiment_assignment_repository,
    )
    experiment_overlay_service = ExperimentOverlayService(
        experiment_service=experiment_service,
    )
    experiment_analysis_service = ExperimentAnalysisService(
        experiment_service=experiment_service,
        turn_record_repository=turn_record_repository,
        provider=active_llm_provider,
        feature_key=FEATURE_EXPERIMENT_ANALYSIS,
    )

    chat_persona_curiosity_service = (
        persona_curiosity_service
        if app_settings.persona.curiosity_enabled
        else None
    )
    chat_persona_curiosity_planner = (
        persona_curiosity_planner
        if app_settings.persona.curiosity_enabled
        else None
    )
    proactive_persona_curiosity_service = (
        persona_curiosity_service
        if (
            app_settings.persona.curiosity_enabled
            and app_settings.persona.curiosity_proactive_enabled
        )
        else None
    )
    proactive_persona_curiosity_planner = (
        persona_curiosity_planner
        if (
            app_settings.persona.curiosity_enabled
            and app_settings.persona.curiosity_proactive_enabled
        )
        else None
    )
    prompt_material_digester = (
        LLMPromptMaterialDigester(
            provider=active_llm_provider,
            feature_key=FEATURE_PROMPT_MATERIAL_DIGEST,
        )
        if (
            app_settings.prompt_quality.material_digest_enabled
            and (
                app_settings.cloud.active
                or app_settings.default_provider_id != _FAKE_PROVIDER_ID
            )
        )
        else NullPromptMaterialDigester()
    )
    # GD1-A: built before the chat service so the same instance the internal
    # drain route flips is the one the turn path reads. Wired unconditionally —
    # self-host simply never receives a drain request, so the flag stays False
    # and every path below reduces to an integer increment.
    drain_state = DrainState()
    chat_service = ChatService(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        post_turn_processor=post_turn_processor,
        prompt_context_builder=prompt_context_builder,
        model_registry=model_registry,
        active_llm_provider=active_llm_provider,
        nsfw_mode_service=nsfw_mode_service,
        state_engine=state_engine,
        goal_service=goal_service,
        goal_reviewer=goal_reviewer,
        daily_goal_review_service=daily_goal_review_service,
        self_repetition_extractor=self_repetition_extractor,
        behavioral_pattern_repository=(
            behavioral_pattern_repository
            if app_settings.humanization.behavioral_pattern_enabled
            else None
        ),
        schedule_service=schedule_service,
        schedule_memorializer=schedule_memorializer,
        feed_reaction_memorializer=feed_reaction_memorializer,
        dialogue_summarizer=dialogue_summarizer,
        embedder=embedder,
        state_tracker=state_tracker,
        auto_consolidation_trigger=auto_consolidation_trigger,
        tool_registry=tool_registry,
        tool_orchestrator=tool_orchestrator,
        story_event_service=story_event_service,
        story_arc_service=story_arc_service,
        # 起幕 (SC1-C): a turn played inside a live scene gets the scene
        # frame in its prompt, bumps the scene's idle clock, and comes
        # back with action chips. Outside a scene neither is touched.
        story_scene_sessions=story_scene_session_repository,
        story_scene_chips_writer=story_scene_chips_writer,
        # SC1-D: an in-scene turn also asks whether the scene just ended,
        # and hands the wrap-up back on the same reply so the player does
        # not have to reload to see the curtain come down.
        story_scene_service=story_scene_service,
        proactive_attempt_repository=proactive_attempt_repository,
        feed_post_repository=feed_post_repository,
        journal_repository=turn_journal_repository,
        extract_in_background=True,
        public_base_url=app_settings.public_base_url,
        uploads_dir=app_settings.uploads_dir,
        object_storage=object_storage,
        operator_profile_service=operator_profile_service,
        idle_drift_judge=idle_drift_judge,
        busy_reply_decider=busy_reply_decider,
        pending_follow_up_repository=pending_follow_up_repository,
        character_encounter_intent_repository=character_encounter_intent_repository,
        persona_extraction_service=persona_extraction_service,
        operator_persona_service=operator_persona_service,
        character_social_knowledge_service=character_social_knowledge_service,
        relationship_seed_repository=relationship_seed_repository,
        persona_curiosity_service=chat_persona_curiosity_service,
        persona_curiosity_planner=chat_persona_curiosity_planner,
        prompt_material_digester=prompt_material_digester,
        prompt_material_digest_enabled=app_settings.prompt_quality.material_digest_enabled,
        register_profiler=register_profiler,
        register_profile_enabled=app_settings.prompt_quality.register_profile_enabled,
        novelty_gate=novelty_gate,
        novelty_gate_enabled=app_settings.prompt_quality.novelty_gate_enabled,
        novelty_gate_max_retries=app_settings.prompt_quality.novelty_gate_max_retries,
        reply_quality_gate_risk_threshold=(
            app_settings.prompt_quality.reply_quality_gate_risk_threshold
        ),
        reply_quality_similarity_threshold=(
            app_settings.prompt_quality.reply_quality_similarity_threshold
        ),
        turn_recorder=turn_recorder,
        usage_recorder=usage_recorder,
        subscription_access_guard=subscription_access_guard,
        # Per-conversation player-turn mutex. Reuses the lease backend already
        # chosen for the studio lease (SA table under the distributed topology,
        # in-process otherwise) but mints a per-turn owner, so two turns exclude
        # each other whether they land on two api replicas or one.
        turn_lease=ChatTurnLease.from_studio_lease(studio_execution_lease),
        drain_state=drain_state,
        action_billing=action_billing_service,
        quota_overage=quota_overage_service,
        emotion_event_repository=emotion_event_repository,
        self_reflection_repository=self_reflection_repository,
        address_preference_repository=(
            address_preference_repository
            if app_settings.humanization.address_preference_enabled
            else None
        ),
        player_persona_note_repository=player_persona_note_repository,
        address_change_log_repository=address_change_log_repository,
        relationship_names_service=relationship_names_service,
        experiment_overlay_service=experiment_overlay_service,
        nsfw_safe_summarizer=nsfw_safe_summarizer,
        account_runtime_profile_resolver=account_runtime_profile_resolver,
        account_runtime_usage_repository=account_runtime_usage_repository,
        event_seed_dispenser=event_seed_dispenser,
        event_mention_repository=character_event_mention_repository,
        world_event_repository=world_event_repository,
        clock=clock,
    )

    # LH2 external-chat turn state machine (DR-LH0-004): the durable receipt
    # store + the recoverable orchestrator that drives ChatService through the
    # fenced execution seam. Wired in every mode — the route's own service-
    # credential gate 503s where unconfigured (self-host), so exposing the
    # service is safe. Needs ``chat_service`` so it is built here, after it.
    if db_session_factory is not None:
        from kokoro_link.infrastructure.persistence.sa_external_chat_turn_repository import (
            SAExternalChatTurnReceiptRepository,
        )
        external_chat_turn_receipt_repository = SAExternalChatTurnReceiptRepository(
            db_session_factory,
        )
    else:
        from kokoro_link.infrastructure.repositories.in_memory_external_chat_turn import (
            InMemoryExternalChatTurnReceiptRepository,
        )
        external_chat_turn_receipt_repository = (
            InMemoryExternalChatTurnReceiptRepository()
        )
    from kokoro_link.application.services.external_chat_turn_service import (
        ExternalChatTurnService,
    )
    external_chat_turn_service = ExternalChatTurnService(
        receipt_repository=external_chat_turn_receipt_repository,
        chat_service=chat_service,
        conversation_repository=conversation_repository,
        roster_service=external_chat_roster_service,
        operator_profile_repository=operator_profile_repository,
        object_storage=object_storage,
    )

    messaging_account_service = MessagingAccountService(
        account_repository=messaging_account_repository,
        binding_repository=channel_binding_repository,
        character_repository=character_repository,
        default_whatsapp_sidecar_url=app_settings.whatsapp_sidecar.base_url,
        default_whatsapp_api_token=app_settings.whatsapp_sidecar.api_token,
    )
    channel_binding_service = ChannelBindingService(
        binding_repository=channel_binding_repository,
        account_repository=messaging_account_repository,
    )
    messaging_public_url_resolver = MessagingPublicUrlResolver(
        preferences_repository=preferences_repository,
        app_public_base_url=app_settings.public_base_url,
    )
    messaging_adapters = _build_messaging_adapters(object_storage=object_storage)

    async def _messaging_operator_language(character_id: str) -> str:
        """Resolve a character's owning-operator content language so the
        dispatcher can localize inbound placeholders + outbound channel
        wrappers. Falls back to zh-TW on any miss."""
        character = await character_repository.get(character_id)
        if character is None:
            return "zh-TW"
        user_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            operator = await operator_profile_service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return "zh-TW"
        language = getattr(operator, "primary_language", "") or ""
        return language.strip() or "zh-TW"

    # Cross-instance inbound dedup. DB-backed only (the claim is a DB
    # unique-index race); ``None`` on the in-memory / no-DB path, where a
    # single process is already fully covered by ``InboundDebouncer`` and
    # behaviour is unchanged.
    from kokoro_link.infrastructure.persistence.sa_inbound_receipts import (
        SAInboundReceiptRepository,
    )

    inbound_receipt_port = (
        SAInboundReceiptRepository(db_session_factory)
        if db_session_factory is not None else None
    )
    # Dispatcher is always wired now — it just does nothing useful until
    # the operator creates at least one MessagingAccount via the UI.
    messaging_dispatcher = MessagingDispatcher(
        account_repository=messaging_account_repository,
        binding_repository=channel_binding_repository,
        conversation_repository=conversation_repository,
        chat_service=chat_service,
        adapters=messaging_adapters,
        debouncer=InboundDebouncer(),
        receipt_repository=inbound_receipt_port,
        public_base_url=app_settings.public_base_url,
        public_base_url_provider=messaging_public_url_resolver.resolve,
        operator_language_resolver=_messaging_operator_language,
    )
    telegram_polling_service = TelegramPollingService(
        account_repository=messaging_account_repository,
        character_repository=character_repository,
        dispatcher=messaging_dispatcher,
        polling_client=TelegramAdapter(),
        update_parser=parse_telegram_update,
        photo_downloader=download_telegram_photo,
        uploads_dir=app_settings.uploads_dir,
        object_storage=object_storage,
    )
    discord_gateway_service = DiscordGatewayService(
        account_repository=messaging_account_repository,
        character_repository=character_repository,
        dispatcher=messaging_dispatcher,
        gateway_client=DiscordGatewayClient(),
        message_parser=parse_discord_message_create,
        attachment_downloader=download_discord_attachment,
        uploads_dir=app_settings.uploads_dir,
        object_storage=object_storage,
    )
    whatsapp_gateway_service = WhatsAppGatewayService(
        account_repository=messaging_account_repository,
        dispatcher=messaging_dispatcher,
        sidecar_client=WhatsAppSidecarClient(),
        event_parser=parse_whatsapp_event,
    )

    proactive_decider = _build_proactive_decider(
        active_provider=active_llm_provider,
    )
    proactive_intention_judge = _build_proactive_intention_judge(
        active_provider=active_llm_provider,
        default_provider_id=app_settings.default_provider_id,
    )
    async def _proactive_schedule_resolver(character, when):
        """Ensure today's schedule before building the proactive context.

        Mirrors the user-chat path: if a schedule hasn't been planned yet
        for the civil day, plan it now (idempotent after the first call).
        Without this the decider would only see ``current_activity=None``
        and generate messages whose "哪裡／正在做什麼" don't line up with
        the schedule view the user is looking at. The planner is expensive
        but ``ensure_schedule`` is a no-op once today's row exists, so the
        cost is paid at most once per character per day."""
        try:
            schedule_obj = await schedule_service.ensure_schedule(character)
        except Exception:
            _LOGGER.exception(
                "proactive schedule ensure failed character=%s", character.id,
            )
            return None, [], None, None
        if schedule_obj is None:
            return None, [], None, None
        current, upcoming, just_finished = schedule_service.resolve_current(
            schedule_obj, now=when,
        )
        return current, upcoming, schedule_obj, just_finished

    # External-event pipeline services — built before the proactive
    # dispatcher so it can claim curated event seeds. Order:
    # fetcher → ingestion service → curator → dispenser → scheduler.
    from kokoro_link.infrastructure.world_event.feedparser_adapter import (
        FeedparserRssAdapter,
    )
    rss_feed_fetcher: RssFeedFetcherPort = FeedparserRssAdapter()
    rss_ingestion_service = RssIngestionService(
        rss_source_repository=rss_source_repository,
        world_event_repository=world_event_repository,
        feed_fetcher=rss_feed_fetcher,
        embedder=embedder,
        event_mention_repository=character_event_mention_repository,
    )
    event_curator_service = EventCuratorService(
        world_event_repository=world_event_repository,
        inbox_repository=character_event_inbox_repository,
        embedder=embedder,
        operator_persona_service=operator_persona_service,
        relationship_seed_repository=relationship_seed_repository,
        # Resolves the owning player's region so the shared event pool is
        # narrowed per player (G3). Absent → no filter, i.e. today's
        # behaviour, which is what self-host keeps.
        operator_profile_service=operator_profile_service,
    )
    world_event_scheduler = WorldEventScheduler(
        ingestion_service=rss_ingestion_service,
        curator_service=event_curator_service,
        character_repository=character_repository,
    )
    from pathlib import Path as _Path
    rss_source_sync_service = RssSourceSyncService(
        repository=rss_source_repository,
        seed_path=_Path(__file__).resolve().parents[1] / "data" / "rss_sources.yaml",
        # Bind region-scoped emergency feeds to the deployment region so
        # an overseas self-host doesn't auto-enable Taiwan-only civil
        # alerts. Passed as the
        # holder's accessor rather than the boot value: a process that came up
        # before the region was corrected would otherwise seed the wrong
        # defaults on any later sync (G0).
        deployment_region=site_settings_holder.calendar_region,
    )

    # Phase 4 realtime outbox wiring (§7.1). Under YURALUME_REALTIME_BACKEND=
    # postgres with a database, the background writer role wraps the two buses
    # with the durable-outbox decorators and the api reader role gets a
    # dispatcher that tails the outbox onto its raw buses (see
    # ``bootstrap/realtime_wiring``). The wrappers are duck-type identical, so
    # every other role / the memory default keeps the raw bus and the container
    # shape is unchanged. Built before the publishers below so the
    # dispatcher/composer publish through the wrapper, not around it.
    from kokoro_link.bootstrap.process_roles import matrix_for_role
    from kokoro_link.bootstrap.realtime_wiring import build_realtime_wiring

    _realtime = build_realtime_wiring(
        matrix=matrix_for_role(app_settings.process.role),
        realtime_backend=app_settings.process.realtime_backend,
        database_url=app_settings.database_url,
        db_session_factory=db_session_factory,
        proactive_bus=ProactiveEventBus(),
        feed_bus=FeedEventBus(),
        conversations=conversation_repository,
        feed_posts=feed_post_repository,
        feed_comments=feed_comment_repository,
        poll_interval=app_settings.process.realtime_poll_interval,
    )
    proactive_event_bus = _realtime.proactive_bus
    feed_event_bus = _realtime.feed_bus
    # LH4 §8.3 — the external proactive sink behind ``ExternalProactiveDeliveryPort``.
    # Self-host (and cloud mode without a configured Channel) wires the self-host
    # messaging adapter (wraps the existing account/binding repos + platform
    # adapters) with NO pre-send ledger. Hosted mode — cloud active AND a Channel
    # base URL AND a database — wires the Hosted Official LINE Channel adapter,
    # the durable pre-send ledger (DR-LH0-005), and the retry worker that re-sends
    # still-pending events; ``_record_pre_send`` / ``_settle_pre_send`` then engage
    # on the dispatcher's hosted path (Core-A left the param).
    from kokoro_link.application.services.proactive_delivery.local_adapter import (
        LocalMessagingProactiveDeliveryAdapter,
    )
    proactive_hosted_delivery = (
        app_settings.cloud.active
        and bool(app_settings.cloud.channel_base_url)
        and db_session_factory is not None
    )
    proactive_event_ledger = None
    proactive_delivery_retry_worker = None
    proactive_hosted_identity_resolver = None
    proactive_local_delivery = None
    if proactive_hosted_delivery:
        from kokoro_link.application.services.proactive_delivery.hosted_adapter import (  # noqa: E501
            HostedChannelProactiveDeliveryAdapter,
        )
        from kokoro_link.application.services.proactive_delivery.hosted_identity import (  # noqa: E501
            build_hosted_delivery_identity_resolver,
        )
        from kokoro_link.application.services.proactive_delivery.retry_worker import (  # noqa: E501
            ProactiveDeliveryRetryWorker,
        )
        from kokoro_link.infrastructure.cloud.hosted_channel_proactive_client import (  # noqa: E501
            HostedChannelProactiveClient,
        )
        from kokoro_link.infrastructure.persistence.sa_external_proactive_events import (  # noqa: E501
            SAExternalProactiveEventRepository,
        )

        # LH4 Core-C — the real reverse identity map: a hosted character is
        # routed to its owner's cloud ``(tenant_id, account_id)`` projection.
        # A character whose owner is not a cloud operator (or whose projection
        # is missing either half) resolves to ``None`` and the dispatcher skips
        # the hosted path with NO_BINDING-gate semantics. Shared by the adapter
        # (cost preflight) and the dispatcher (gate + envelope identity).
        proactive_hosted_identity_resolver = (
            build_hosted_delivery_identity_resolver(
                character_repository=character_repository,
                operator_repository=operator_profile_repository,
            )
        )

        proactive_event_ledger = SAExternalProactiveEventRepository(
            db_session_factory,
        )
        proactive_external_delivery = HostedChannelProactiveDeliveryAdapter(
            client=HostedChannelProactiveClient(
                base_url=app_settings.cloud.channel_base_url,
                service_credential=app_settings.cloud.channel_service_credential,
            ),
            object_storage=object_storage,
            identity_resolver=proactive_hosted_identity_resolver,
            event_ledger=proactive_event_ledger,
        )
        # H4: hosted mode still needs the self-host binding path for any
        # operator who bound their own bot. The local binding path routes to
        # THIS local adapter; the hosted-target path routes to the hosted
        # adapter above. Both are injected so neither ever feeds the other's
        # identity.
        proactive_local_delivery = LocalMessagingProactiveDeliveryAdapter(
            account_repository=messaging_account_repository,
            binding_repository=channel_binding_repository,
            conversation_repository=conversation_repository,
            adapters=messaging_adapters,
        )
        from kokoro_link.application.services.proactive_delivery.line_conversation_recorder import (  # noqa: E501
            HostedLineConversationRecorder,
        )
        proactive_delivery_retry_worker = ProactiveDeliveryRetryWorker(
            ledger=proactive_event_ledger,
            external_delivery=proactive_external_delivery,
            conversation_recorder=HostedLineConversationRecorder(
                conversation_repository=conversation_repository,
            ),
            clock=clock,
        )
    else:
        proactive_external_delivery = LocalMessagingProactiveDeliveryAdapter(
            account_repository=messaging_account_repository,
            binding_repository=channel_binding_repository,
            conversation_repository=conversation_repository,
            adapters=messaging_adapters,
        )
    proactive_dispatcher = ProactiveDispatcher(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        account_repository=messaging_account_repository,
        binding_repository=channel_binding_repository,
        attempt_repository=proactive_attempt_repository,
        gate=HeuristicProactiveGate(local_tz=local_tz),
        decider=proactive_decider,
        adapters=messaging_adapters,
        external_delivery=proactive_external_delivery,
        local_delivery=proactive_local_delivery,
        proactive_event_ledger=proactive_event_ledger,
        hosted_identity_resolver=proactive_hosted_identity_resolver,
        intention_judge=proactive_intention_judge,
        schedule_resolver=_proactive_schedule_resolver,
        memory_repository=memory_repository,
        goal_repository=goal_repository,
        story_event_service=story_event_service,
        story_arc_service=story_arc_service,
        state_tracker=state_tracker,
        rest_recovery_refresher=rest_recovery_refresher,
        tool_registry=tool_registry,
        tool_orchestrator=tool_orchestrator,
        event_bus=proactive_event_bus,
        public_base_url=app_settings.public_base_url,
        public_base_url_provider=messaging_public_url_resolver.resolve,
        local_tz=local_tz,
        dialogue_summarizer=dialogue_summarizer,
        event_seed_dispenser=event_seed_dispenser,
        event_mention_repository=character_event_mention_repository,
        calendar_context_port=calendar_provider,
        weather_context_port=weather_provider,
        schedule_service=schedule_service,
        operator_persona_service=operator_persona_service,
        relationship_seed_repository=relationship_seed_repository,
        persona_curiosity_service=proactive_persona_curiosity_service,
        persona_curiosity_planner=proactive_persona_curiosity_planner,
        operator_profile_service=operator_profile_service,
        turn_recorder=turn_recorder,
        emotion_event_repository=emotion_event_repository,
        deferred_intent_service=deferred_intent_service,
        address_preference_repository=(
            address_preference_repository
            if app_settings.humanization.address_preference_enabled
            else None
        ),
        clock=clock,
        prompt_pack_hash_provider=lambda: get_default_loader().prompt_pack_hash(
            prompt_pack_hash_snapshot(
                app_settings.humanization,
                app_settings.prompt_quality,
            ),
        ),
        notification_service=notification_service,
        register_profiler=register_profiler,
        register_profile_enabled=app_settings.prompt_quality.register_profile_enabled,
        reply_quality_gate=novelty_gate,
        reply_quality_gate_enabled=app_settings.prompt_quality.novelty_gate_enabled,
        reply_quality_gate_max_retries=(
            app_settings.prompt_quality.novelty_gate_max_retries
        ),
        subscription_access_guard=subscription_access_guard,
        visible_slot_port=visible_slot_port,
        # SC1-E — a character inside a 起幕 scene does not also message the
        # player from outside it. Pause only; the
        # tick after the scene closes evaluates exactly as it did before.
        story_scene_sessions=story_scene_session_repository,
        player_persona_note_repository=player_persona_note_repository,
    )
    # Phase 3 of SCENE_BEAT_PLAN — runs on every tick so an offline
    # user still sees beats land in memory by the time they come back.
    beat_due_checker = BeatDueChecker(
        story_event_service=story_event_service,
        story_arc_service=story_arc_service,
        story_beat_scene_service=story_beat_scene_service,
        local_tz=local_tz,
        operator_profile_service=operator_profile_service,
    )
    # ``feed_event_bus`` was built (and possibly outbox-wrapped) up front with
    # the proactive bus in the Phase 4 realtime wiring above.
    feed_candidate_collector = FeedCandidateCollector(
        feed_posts=feed_post_repository,
        schedules=schedule_repository,
        story_arcs=story_arc_repository,
        memories=memory_repository,
        conversations=conversation_repository,
        event_seed_dispenser=event_seed_dispenser,
    )
    # Tell the composer whether the deployment has a video backend ready —
    # drives whether the LLM prompt mentions the ``media_kind`` /
    # ``video_prompt`` fields. Without this the model would pick
    # ``media_kind=video`` on a deploy that can't render it, costing tokens
    # for no reason. The composer itself short-circuits while fake is active.
    #
    # CV0-1: ``bool(video_profile_registry.profile_ids)`` alone is a
    # self-host-only signal — that registry is materialised from
    # ``KOKORO_VIDEO_*`` env, which hosted deployments never set (cloud mode
    # routes video through the Gateway, wired as ``CloudActiveVideoProvider``
    # just above), so hosted never offered the model a video option at all.
    #
    # In cloud mode the answer is ``video_jobs_possible``, not "a cloud
    # provider exists": with the knob off, the same adapter still resolves,
    # but only through the *synchronous* ``/v1/videos/generations`` route —
    # a single await of up to 30 minutes that holds the ``image`` capability
    # slot the whole time. Offering video there would not enable a feature,
    # it would let one post wedge the deployment's background media lane.
    # So video is offered exactly when a render can be queued, which is also
    # exactly when a poll carrier exists to finish it.
    #
    # Self-host (cloud inactive) keeps reading the local registry, unchanged.
    video_jobs_possible = video_jobs_enabled and app_settings.cloud.active
    feed_video_enabled = (
        video_jobs_possible if app_settings.cloud.active
        else bool(video_profile_registry.profile_ids)
    )
    feed_composer_port = LLMFeedComposer(
        provider=active_llm_provider, feature_key=FEATURE_FEED_COMPOSE,
        video_enabled=feed_video_enabled,
    )
    feed_composer_service = FeedComposerService(
        repository=feed_post_repository,
        candidates=feed_candidate_collector,
        composer=feed_composer_port,
        event_bus=feed_event_bus,
        image_provider=active_image_provider,
        video_provider=active_video_provider,
        uploads_dir=app_settings.uploads_dir,
        object_storage=object_storage,
        memory_repository=memory_repository,
        embedder=embedder,
        event_seed_dispenser=event_seed_dispenser,
        schedule_service=schedule_service,
        calendar_context_port=calendar_provider,
        weather_context_port=weather_provider,
        operator_profile_service=operator_profile_service,
        visual_style_service=visual_generation_style_service,
        usage_recorder=usage_recorder,
        notification_service=notification_service,
        register_profiler=register_profiler,
        register_profile_enabled=app_settings.prompt_quality.register_profile_enabled,
        reply_quality_gate=novelty_gate,
        reply_quality_gate_enabled=app_settings.prompt_quality.novelty_gate_enabled,
        reply_quality_gate_max_retries=(
            app_settings.prompt_quality.novelty_gate_max_retries
        ),
        account_runtime_profile_resolver=account_runtime_profile_resolver,
        account_runtime_usage_repository=account_runtime_usage_repository,
        character_repository=character_repository,
        quota_overage=quota_overage_service,
    )
    # CV4 deferred video pipeline. Built unconditionally (it is inert without
    # a provider that takes jobs) but its two *carriers* are not: the embedded
    # sweep below and the distributed poll handler are wired only when the
    # deployment can actually queue a render, so a self-host tick gains
    # nothing — not even one query.
    feed_video_job_service = FeedVideoJobService(
        pending_repository=pending_feed_video_repository,
        video_provider=active_video_provider,
        lander=feed_composer_service,
        storyboard_service=VideoStoryboardService(
            ModelResolver(
                provider=active_llm_provider,
                feature_key=FEATURE_VIDEO_STORYBOARD,
            ),
            object_storage=object_storage,
            public_base_url=app_settings.public_base_url,
            uploads_dir=app_settings.uploads_dir,
        ),
        character_repository=character_repository,
        timeout_seconds=_env_int(
            "KOKORO_VIDEO_JOB_TIMEOUT_SECONDS",
            DEFAULT_VIDEO_JOB_TIMEOUT_SECONDS,
        ),
        clock=clock,
    )
    # Set rather than injected: the composer submits jobs and the pipeline
    # lands their posts through the composer, so exactly one of the two edges
    # has to be closed after construction.
    #
    # ``video_jobs_possible`` (computed with ``feed_video_enabled`` above) is
    # also what tells the composer whether a pending row can exist at all —
    # the tick's in-flight dedup probe is skipped entirely when it cannot,
    # so a self-host tick gains no query. The service's own capability probe
    # remains the authority at call time (an adapter can be configured and
    # still refuse); this flag only decides whether the sweep, the due-job
    # handler and that probe exist at all.
    feed_composer_service.set_video_job_service(
        feed_video_job_service,
        deferred_pipeline_possible=video_jobs_possible,
    )
    feed_reaction_service = FeedReactionService(
        post_repository=feed_post_repository,
        reaction_repository=feed_reaction_repository,
    )
    feed_comment_service = FeedCommentService(
        post_repository=feed_post_repository,
        comment_repository=feed_comment_repository,
    )
    feed_comment_reply_composer = LLMFeedCommentReplyComposer(
        provider=active_llm_provider,
        feature_key=FEATURE_FEED_COMMENT_REPLY,
    )
    feed_comment_reply_service = FeedCommentReplyService(
        post_repository=feed_post_repository,
        comment_repository=feed_comment_repository,
        comment_service=feed_comment_service,
        composer=feed_comment_reply_composer,
        memory_repository=memory_repository,
        embedder=embedder,
        schedule_service=schedule_service,
        local_tz=local_tz,
        character_repository=character_repository,
        event_bus=feed_event_bus,
        operator_profile_service=operator_profile_service,
        notification_service=notification_service,
        visible_slot_port=visible_slot_port,
        player_persona_note_repository=player_persona_note_repository,
    )
    # PF1 — the two-pass compose→tool→compose loop shared by every kind of
    # pending follow-up. Same registry / orchestrator / public-URL resolver the
    # proactive path uses, so a promised photo is delivered through exactly the
    # same absolutisation rules as a spontaneous one.
    promise_tool_loop = ComposerToolLoop(
        tool_registry=tool_registry,
        tool_orchestrator=tool_orchestrator,
        public_base_url=app_settings.public_base_url,
        public_base_url_provider=messaging_public_url_resolver.resolve,
        surface="promise",
        # The same ceilings the worker claims under. A capability capped at 0
        # is a closed queue, so a fulfilment that would have to hand that
        # capability off is not offered the tool at all — it says so in words
        # instead of waiting forever for a job nobody can claim. Only the
        # deferring (distributed) caller is filtered; the embedded release
        # runs its tools inline and is unaffected whatever the env says.
        capability_caps=_capability_caps(),
    )
    pending_follow_up_dispatcher = PendingFollowUpDispatcher(
        repository=pending_follow_up_repository,
        composer=pending_follow_up_composer,
        proactive_dispatcher=proactive_dispatcher,
        tool_loop=promise_tool_loop,
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        schedule_service=schedule_service,
        dialogue_summarizer=dialogue_summarizer,
        scheduled_promise_composer=scheduled_promise_composer,
        operator_persona_service=operator_persona_service,
        operator_profile_service=operator_profile_service,
        # At-most-once visible send for the distributed release path (shared with
        # proactive/feed). ``None`` on the no-DB path → fail-open, embedded tick
        # byte-identical.
        visible_slot_port=visible_slot_port,
        local_tz=local_tz,
        player_persona_note_repository=player_persona_note_repository,
    )
    # P2-B shadow runtime (HOSTED_CORE_SCALING §13 Phase 2). Built ONLY for a
    # scheduler-owning role (all / background) when YURALUME_BACKGROUND_SHADOW=
    # postgres and a DB session factory exists. Off by default → every field
    # stays None, the scheduler gets no journal, and behaviour is byte-identical
    # (self-host red line). The SA journal is threaded into the scheduler below
    # so the embedded tick can be compared bucket-for-bucket with the queue.
    (
        background_job_queue,
        background_coordinator_lease,
        background_shadow_coordinator,
        background_shadow_worker,
        shadow_tick_journal,
        shadow_bucket_seconds,
    ) = _build_shadow_runtime(
        app_settings=app_settings,
        db_session_factory=db_session_factory,
        character_repository=character_repository,
        operator_profile_repository=operator_profile_repository,
        subscription_access_guard=subscription_access_guard,
        clock=clock,
    )

    # Execution-ownership gate (P3-B, §2.2 / §15). Built ONLY on the hosted
    # opt-in: background_backend=='postgres' AND a DB session factory exists.
    # On the self-host default (embedded backend) the port stays None and
    # ``ownership_enforced`` is False, so the embedded scheduler NEVER reads
    # ownership — zero DB calls, byte-identical. (from_env still rejects the
    # postgres backend until P3-C; this path is reached via direct AppSettings
    # construction in tests / the future unlock.)
    runtime_ownership: "RuntimeOwnershipPort | None" = None
    execution_mode_transition: "ExecutionModeTransitionService | None" = None
    ownership_enforced = (
        app_settings.process.background_backend == "postgres"
        and db_session_factory is not None
    )
    if ownership_enforced:
        from kokoro_link.infrastructure.persistence.sa_background_runtime import (
            SABackgroundExecutionMode,
        )

        assert db_session_factory is not None
        runtime_ownership = SABackgroundExecutionMode(db_session_factory)
        from kokoro_link.infrastructure.persistence.sa_background_runtime import (
            SABackgroundCoordinatorCursor,
        )
        from kokoro_link.application.services.execution_mode_transition import (
            ExecutionModeTransitionService,
        )

        execution_mode_transition = ExecutionModeTransitionService(
            ownership=runtime_ownership,
            queue=background_job_queue,
            cursor=SABackgroundCoordinatorCursor(db_session_factory),
            clock=clock,
        )
        if background_shadow_coordinator is not None:
            background_shadow_coordinator.set_runtime_ownership(runtime_ownership)

    # Dedicated hosted coordinators own the site-global world-event loop, but
    # every scheduled pass must confirm the same durable lease/epoch used by
    # due-job coordination. Embedded all/background roles keep no guard.
    if (
        process_matrix.start_world_event_scheduler
        and not process_matrix.start_schedulers
        and background_shadow_coordinator is not None
    ):
        world_event_scheduler.set_leadership_guard(
            background_shadow_coordinator.owns_live_lease,
        )

    # SC1-E — the idle 起幕 wrap-up. One instance serves both scheduling
    # lines: the embedded tick executor calls it per character, and the
    # distributed ``story_scene_timeout`` chain calls the same step. The
    # window is the single configurable number in this feature
    # (24h by default); everything downstream —
    # the step, the chain's explicit due time, the unowned sweep — reads it
    # from this one object rather than re-deriving it.
    story_scene_timeout_closer = StorySceneTimeoutCloser(
        sessions=story_scene_session_repository,
        scenes=story_scene_service,
        characters=character_repository,
        idle_timeout_seconds=_env_int(
            "YURALUME_STORY_SCENE_IDLE_TIMEOUT_SECONDS",
            int(DEFAULT_STORY_SCENE_IDLE_TIMEOUT_SECONDS),
        ),
    )

    scheduler_metrics = SchedulerMetrics()
    proactive_scheduler = ProactiveScheduler(
        dispatcher=proactive_dispatcher,
        character_repository=character_repository,
        rest_recovery_refresher=rest_recovery_refresher,
        beat_due_checker=beat_due_checker,
        schedule_service=schedule_service,
        feed_composer=feed_composer_service,
        feed_comment_reply=feed_comment_reply_service,
        pending_follow_up_dispatcher=pending_follow_up_dispatcher,
        proactive_delivery_retry_worker=proactive_delivery_retry_worker,
        # Embedded carrier for the deferred video pipeline, wired only where a
        # render can actually be queued. On self-host this is ``None`` and the
        # tick body is byte-identical to what it was before CV4.
        feed_video_job_service=(
            feed_video_job_service if video_jobs_possible else None
        ),
        character_encounter_service=character_encounter_service,
        character_social_knowledge_service=character_social_knowledge_service,
        schedule_memorializer=schedule_memorializer,
        schedule_weather_drift=schedule_weather_drift_service,
        goal_review_service=daily_goal_review_service,
        story_scene_timeout_closer=story_scene_timeout_closer,
        persona_dream_service=persona_dream_service,
        persona_dream_repository=persona_repository,
        account_runtime_profile_resolver=account_runtime_profile_resolver,
        clock=clock,
        subscription_access_guard=subscription_access_guard,
        metrics=scheduler_metrics,
        tick_journal=shadow_tick_journal,
        bucket_seconds=shadow_bucket_seconds,
        runtime_ownership=runtime_ownership,
        ownership_enforced=ownership_enforced,
    )

    # Phase 5: per-kind due-job wiring (§4.2 / §4.3 / §5). Built on the same
    # hosted opt-in as execution. The reconciler is injected into the coordinator
    # (reseeds missing chains, ~15 min); the per-kind handler is injected into the
    # worker's execution runner (step + self-chain). Both share ONE
    # NextDueCalculator and the coordinator's leader epoch. Self-host / embedded
    # never reaches here (ownership_enforced is False), so behaviour is unchanged.
    if (
        ownership_enforced
        and background_job_queue is not None
        and background_shadow_coordinator is not None
    ):
        from kokoro_link.application.services.due_job_scheduler import (
            NextDueCalculator,
        )
        from kokoro_link.application.services.due_job_reconciler import (
            DueJobReconciler,
        )
        from kokoro_link.application.services.social_due_job_reconciler import (
            SocialDueJobReconciler,
        )
        from kokoro_link.application.services.pending_follow_up_release import (
            PendingFollowUpReleaseEnqueuer,
            PendingFollowUpReleaseReconciler,
        )
        from kokoro_link.application.services.post_turn_runner import (
            PostTurnEnqueuer,
        )

        # Event-driven follow-up release (§5 priority 1). ONE enqueuer is shared by
        # the chat write points (via a ChatService setter) and the reconcile sweep,
        # so both mint the same idempotent release job. The enqueuer reads the
        # current coordinator epoch from the lease (the API process holds no lease).
        assert background_coordinator_lease is not None
        _release_enqueuer = PendingFollowUpReleaseEnqueuer(
            queue=background_job_queue,
            coordinator_lease=background_coordinator_lease,
            clock=clock,
        )
        chat_service.set_pending_follow_up_release_enqueuer(_release_enqueuer)
        # PF3 — the SAME enqueuer is how a fulfilment hands its GPU half to the
        # image-capped kind. Only wired here, i.e. only where a distributed queue
        # exists: with no queue there is nowhere to defer to and the tool runs
        # inline, which is exactly what self-host wants.
        pending_follow_up_dispatcher.set_capability_release_enqueuer(
            _release_enqueuer,
        )

        # Event-driven post-turn processing (§5 priority 3). The chat write point
        # enqueues ONE immediate one-shot job so the post-turn LLM extraction runs on
        # a worker instead of inside the API request; a no-leader enqueue no-ops and
        # the write point falls back to in-process execution (no dropped work).
        chat_service.set_post_turn_enqueuer(PostTurnEnqueuer(
            queue=background_job_queue,
            coordinator_lease=background_coordinator_lease,
            clock=clock,
        ))
        _follow_up_reconciler = PendingFollowUpReleaseReconciler(
            repository=pending_follow_up_repository,
            enqueuer=_release_enqueuer,
            clock=clock,
        )

        # CV4: the deferred video poll's event-driven carrier. The enqueuer is
        # handed to the pipeline itself (which mints the next observation from
        # inside submit / poll) and to the reconcile sweep, so both produce the
        # same idempotent job. Only wired where a render can be queued.
        _feed_video_reconciler = None
        if video_jobs_possible:
            from kokoro_link.application.services.feed_video_poll_jobs import (
                FeedVideoPollEnqueuer,
                FeedVideoPollReconciler,
            )

            _feed_video_enqueuer = FeedVideoPollEnqueuer(
                queue=background_job_queue,
                coordinator_lease=background_coordinator_lease,
                clock=clock,
            )
            feed_video_job_service.set_poll_enqueuer(_feed_video_enqueuer)
            _feed_video_reconciler = FeedVideoPollReconciler(
                repository=pending_feed_video_repository,
                enqueuer=_feed_video_enqueuer,
                clock=clock,
            )

        # §13 social split: relink missing social chains (per character dream/peer
        # + per pair encounter) on the same leader + cadence as the character
        # reconcile. Shares the ONE NextDueCalculator and the coordinator's epoch.
        _reconcile_next_due = NextDueCalculator(
            resolver=due_job_profile_resolver, clock=clock,
        )
        _reseed_jitter = _env_int("YURALUME_DUE_RESEED_JITTER", 300)
        _social_reconciler = SocialDueJobReconciler(
            queue=background_job_queue,
            character_repository=character_repository,
            relationship_repository=character_relationship_repository,
            next_due_calculator=_reconcile_next_due,
            epoch_provider=lambda: background_shadow_coordinator.epoch,
            runtime_ownership=runtime_ownership,
            operator_profile_repository=operator_profile_repository,
            clock=clock,
            reseed_jitter_seconds=_reseed_jitter,
        )
        # Reconciler → coordinator (only where a coordinator runs in this
        # process). Shares the coordinator's leader epoch via ``epoch_provider``.
        _reconciler = DueJobReconciler(
            queue=background_job_queue,
            character_repository=character_repository,
            next_due_calculator=_reconcile_next_due,
            epoch_provider=lambda: background_shadow_coordinator.epoch,
            runtime_ownership=runtime_ownership,
            operator_profile_repository=operator_profile_repository,
            clock=clock,
            reseed_jitter_seconds=_reseed_jitter,
            follow_up_reconciler=_follow_up_reconciler,
            social_reconciler=_social_reconciler,
            feed_video_reconciler=_feed_video_reconciler,
        )
        background_shadow_coordinator.set_due_job_reconciler(
            _reconciler,
            interval_seconds=float(
                _env_int("YURALUME_DUE_RECONCILE_INTERVAL", 900),
            ),
        )

    # P3-C: upgrade the shadow worker from dry-run to mode-aware execution on the
    # hosted opt-in (background_backend=='postgres' AND DB AND this role runs the
    # scheduler). The runner reuses the SCHEDULER's executors so the distributed
    # tick body is byte-identical to embedded; it flips to real execution only
    # when the ownership row confirms ``distributed`` (fail-closed to dry-run).
    if (
        ownership_enforced
        and background_shadow_worker is not None
        and runtime_ownership is not None
    ):
        from kokoro_link.application.services.execution_mode_runner import (
            ExecutionModeRunner,
        )
        from kokoro_link.application.services.runtime_activity_gate import (
            RuntimeActivityGateService,
        )
        from kokoro_link.application.services.due_job_scheduler import (
            NextDueCalculator,
        )
        from kokoro_link.application.services.due_job_handlers import (
            CharacterKindHandler,
        )
        from kokoro_link.application.services.social_due_job_handlers import (
            SocialKindHandler,
        )
        from kokoro_link.application.services.due_job_deferral import (
            DueJobDeferral,
        )
        from kokoro_link.infrastructure.persistence.sa_background_runtime import (
            SABackgroundCoordinatorCursor,
        )
        from kokoro_link.infrastructure.persistence.sa_pair_lease import (
            SAPairLease,
        )
        import socket as _socket

        assert db_session_factory is not None
        assert background_job_queue is not None
        _worker_next_due = NextDueCalculator(
            # Same short-TTL memo as the reconciler's: the worker now resolves
            # a profile in every handler pre-flight (NF4 dormancy) as well as
            # in every chain advance.
            resolver=due_job_profile_resolver, clock=clock,
        )
        _worker_deferral = DueJobDeferral(
            feed_post_repository=feed_post_repository,
            operator_profile_service=operator_profile_service,
        )
        # The worker's handler enqueues its next chain link under the CLAIMED
        # job's fencing epoch (cross-process — it does not hold the coordinator
        # lease); ``epoch_provider`` is a passive fallback (no local coordinator
        # in a dedicated worker role → None → reconciler owns reseeding).
        _worker_epoch_provider = (
            (lambda: background_shadow_coordinator.epoch)
            if background_shadow_coordinator is not None
            else (lambda: None)
        )
        _character_kind_handler = CharacterKindHandler(
            executor=proactive_scheduler.character_tick_executor,
            queue=background_job_queue,
            next_due_calculator=_worker_next_due,
            epoch_provider=_worker_epoch_provider,
            operator_profile_repository=operator_profile_repository,
            deferral_check=_worker_deferral.check,
            beat_next_due_provider=beat_due_checker.next_due_at,
            # An open scene's idle deadline is exact, so the chain jumps to
            # it instead of rechecking hourly until the window has passed.
            scene_timeout_due_provider=(
                story_scene_timeout_closer.next_timeout_at
            ),
            clock=clock,
        )
        # §13 social split handler: the pair lease (atomic two-character guard)
        # backs ``encounter_tick``; ``persona_dream`` / ``peer_knowledge`` are
        # per-character. Owner id is stable per worker process so a same-process
        # re-acquire renews the same epoch.
        _social_kind_handler = SocialKindHandler(
            social_executor=proactive_scheduler.social_tick_executor,
            encounter_service=character_encounter_service,
            queue=background_job_queue,
            next_due_calculator=_worker_next_due,
            epoch_provider=_worker_epoch_provider,
            pair_lease=SAPairLease(db_session_factory),
            pair_lease_owner=f"social-worker-{_socket.gethostname()}-{os.getpid()}",
            operator_profile_repository=operator_profile_repository,
            clock=clock,
        )
        # §5 priority 1: the worker-side follow-up release handler delegates the
        # busy re-gate + compose + fan-out to the SAME dispatcher the embedded tick
        # uses (抽共用而非複製).
        from kokoro_link.application.services.pending_follow_up_release import (
            PendingFollowUpReleaseHandler,
        )
        from kokoro_link.application.services.post_turn_runner import (
            PostTurnHandler,
        )

        _release_handler = PendingFollowUpReleaseHandler(
            repository=pending_follow_up_repository,
            dispatcher=pending_follow_up_dispatcher,
            clock=clock,
        )
        # §5 priority 3: the worker-side post-turn handler delegates to the SAME
        # ``_do_post_turn`` the embedded tick runs, via the id-only rebuild entry
        # (抽共用而非複製). It re-reads the conversation to rebuild the turn text so the
        # job payload never carries chat content.
        _post_turn_handler = PostTurnHandler(
            runner=chat_service.run_post_turn_for_record,
            clock=clock,
        )
        # CV4: the worker-side poll delegates to the SAME pipeline object the
        # embedded sweep drives (抽共用而非複製) — one implementation of
        # "observe, download, publish or degrade", two carriers.
        _feed_video_poll_handler = None
        if video_jobs_possible:
            from kokoro_link.application.services.feed_video_poll_jobs import (
                FeedVideoPollHandler,
            )

            _feed_video_poll_handler = FeedVideoPollHandler(
                service=feed_video_job_service, clock=clock,
            )
        execution_runner = ExecutionModeRunner(
            queue=background_job_queue,
            character_repository=character_repository,
            character_tick_executor=proactive_scheduler.character_tick_executor,
            social_tick_executor=proactive_scheduler.social_tick_executor,
            gate_service=RuntimeActivityGateService(
                resolver=account_runtime_profile_resolver,
                character_repository=character_repository,
            ),
            cursor=SABackgroundCoordinatorCursor(db_session_factory),
            bucket_seconds=shadow_bucket_seconds or 300,
            clock=clock,
            runtime_ownership=runtime_ownership,
            character_kind_handler=_character_kind_handler,
            social_kind_handler=_social_kind_handler,
            character_relationship_repository=character_relationship_repository,
            pending_follow_up_release_handler=_release_handler,
            post_turn_handler=_post_turn_handler,
            feed_video_poll_handler=_feed_video_poll_handler,
        )
        background_shadow_worker.enable_execution(
            runtime_ownership=runtime_ownership,
            execution_runner=execution_runner,
        )
        # §5 caps bind at claim time (execution mode only).
        background_shadow_worker.set_capability_caps(_capability_caps())

    tts_voice_catalog: TTSVoiceCatalogPort | None = None
    tts_settings = app_settings.tts
    if (
        app_settings.cloud.active
        and process_matrix.requires_cloud_provider_credentials
    ):
        assert cloud_identity_resolver is not None
        tts_settings = TTSSettings(
            provider="api",
            base_url=app_settings.cloud.gateway_url,
            api_key=app_settings.cloud.deployment_token,
            voice_id=app_settings.cloud.tts_voice_default,
            timeout_seconds=app_settings.tts.timeout_seconds,
        )
        tts_port = CloudGatewayTTSAdapter(
            base_url=app_settings.cloud.gateway_url,
            deployment_token=app_settings.cloud.deployment_token,
            deployment_id=app_settings.cloud.deployment_id,
            audience=app_settings.cloud.deployment_audience,
            default_voice_id=app_settings.cloud.tts_voice_default,
            character_repository=character_repository,
            identity_resolver=cloud_identity_resolver,
            routing_profile_port=cloud_routing_profile_resolver,
            timeout_seconds=app_settings.tts.timeout_seconds,
        )
        tts_voice_catalog = tts_port
    elif app_settings.cloud.active:
        tts_port = NullTTSAdapter()
    elif not app_settings.tts.enabled:
        tts_port = NullTTSAdapter()
    elif app_settings.tts.provider == "openai":
        tts_port = OpenAITTSAdapter(
            api_key=app_settings.tts.api_key,
            model=app_settings.tts.model or "gpt-4o-mini-tts",
            default_voice_id=app_settings.tts.voice_id or "marin",
            response_format=app_settings.tts.response_format,
            base_url=app_settings.tts.base_url or "https://api.openai.com/v1",
            timeout_seconds=app_settings.tts.timeout_seconds,
        )
        tts_voice_catalog = tts_port
    else:
        tts_port = ExternalTTSAdapter(
            base_url=app_settings.tts.base_url,
            api_key=app_settings.tts.api_key,
            default_voice_id=app_settings.tts.voice_id,
            timeout_seconds=app_settings.tts.timeout_seconds,
        )
        tts_voice_catalog = tts_port
    tts_translator = LLMTTSTranslator(
        provider=active_llm_provider,
        feature_key=FEATURE_TTS_TRANSLATE,
    )
    tts_service = TTSService(
        port=tts_port,
        settings=tts_settings,
        uploads_dir=app_settings.uploads_dir,
        object_storage=object_storage,
        translator=tts_translator,
        character_repository=character_repository,
        usage_recorder=usage_recorder,
        account_runtime_profile_resolver=account_runtime_profile_resolver,
        subscription_access_guard=subscription_access_guard,
    )
    if app_settings.cloud.active:
        # Cloud mode charges TTS at the moment the player presses play
        # (TTSService.synthesize, called from the play-triggered
        # route) — that request is the billing point. Background
        # pregeneration runs before any button press, so it would call the
        # paid upstream TTS provider with nobody to charge, and if the
        # player later does press play the pregenerated result is already
        # cached, so the "charge on play" request becomes a cache hit that
        # never reaches the metered synthesize call. Gating here — the
        # single place cloud vs self-host wiring already branches — means
        # ``ChatService`` never receives a pregenerator in cloud mode and
        # both call sites' existing ``is None`` guard makes this a no-op,
        # with zero behavior change for self-host.
        tts_pregeneration_service = None
    else:
        tts_pregeneration_service = TTSPregenerationService(
            tts_service=tts_service,
            preferences=preferences_repository,
        )
        chat_service.set_tts_pregenerator(tts_pregeneration_service)
    turn_undo_service = TurnUndoService(
        journal_repository=turn_journal_repository,
        conversation_repository=conversation_repository,
        character_repository=character_repository,
        memory_repository=memory_repository,
        state_history_repository=state_history_repository,
        goal_repository=goal_repository,
        arc_repository=story_arc_repository,
        schedule_repository=schedule_repository,
        operator_persona_repository=persona_repository,
    )

    # CD2 — the character-delete boundary engine. Built here (not inside
    # CharacterService) because only the container knows which of the two
    # persistence modes is live and which repositories exist at all.
    character_data_erasure = _build_character_data_erasure(
        db_session_factory=db_session_factory,
        object_storage=object_storage,
        character_repository=character_repository,
        # In-memory mode only — the DB mode ignores the whole mapping.
        # This is the **authoritative** slot wiring: every repository this
        # container built that the delete boundary reaches. Slot names and
        # execution order live in ``CHARACTER_ERASURE_REPOSITORY_SLOTS``
        # (which also records why ``story_seed`` is absent); this site
        # only says which object fills each slot.
        repositories={
            "state_history": state_history_repository,
            "goal": goal_repository,
            "schedule": schedule_repository,
            "memory": memory_repository,
            "memoir_pin": memoir_pin_repository,
            "proactive_attempt": proactive_attempt_repository,
            "tool_invocation": tool_invocation_repository,
            "event_mention": character_event_mention_repository,
            "turn_journal": turn_journal_repository,
            "album": album_repository,
            "feed_post": feed_post_repository,
            "story_event": story_event_repository,
            "story_arc": story_arc_repository,
            "operator_persona": persona_repository,
            "relationship_seed": relationship_seed_repository,
            "emotion_event": emotion_event_repository,
            "behavioral_pattern": behavioral_pattern_repository,
            "self_reflection": self_reflection_repository,
            "disposition_drift_history": disposition_drift_history_repository,
            "messaging_account": messaging_account_repository,
            "character_relationship": character_relationship_repository,
            "character_peer_profile": character_peer_profile_repository,
            "character_encounter": character_encounter_repository,
            "character_encounter_intent": character_encounter_intent_repository,
            "pending_follow_up": pending_follow_up_repository,
            "conversation": conversation_repository,
        },
    )
    # CD3 — the character-reset boundary engine, same reasoning as CD2's
    # erasure engine above: only the container knows which persistence
    # mode is live.
    character_data_reset = _build_character_data_reset(
        db_session_factory=db_session_factory,
        memory_repository=memory_repository,
        conversation_repository=conversation_repository,
        state_history_repository=state_history_repository,
        operator_persona_repository=persona_repository,
    )
    character_service = CharacterService(
        character_repository,
        character_data_erasure=character_data_erasure,
        character_data_reset=character_data_reset,
        conversation_repository=conversation_repository,
        memory_repository=memory_repository,
        goal_repository=goal_repository,
        schedule_repository=schedule_repository,
        state_history_repository=state_history_repository,
        proactive_attempt_repository=proactive_attempt_repository,
        tool_invocation_repository=tool_invocation_repository,
        album_repository=album_repository,
        story_arc_repository=story_arc_repository,
        pending_follow_up_repository=pending_follow_up_repository,
        operator_persona_repository=persona_repository,
        relationship_seed_repository=relationship_seed_repository,
        state_tracker=state_tracker,
        rest_recovery_refresher=rest_recovery_refresher,
        emotion_event_repository=emotion_event_repository,
        arc_series_repository=arc_series_repository,
        arc_template_repository=arc_template_repository,
        account_runtime_profile_resolver=account_runtime_profile_resolver,
        account_runtime_usage_repository=account_runtime_usage_repository,
        clock=clock,
        subscription_access_guard=subscription_access_guard,
        # P3-C durable warmup: enqueue a character_warmup job on create so a new
        # character is ready before its first bucket. Wired ONLY on the hosted
        # execution backend (queue + coordinator lease present); self-heals via
        # the next character_tick if the enqueue is fenced out / fails.
        background_job_queue=background_job_queue,
        background_coordinator_lease=background_coordinator_lease,
        warmup_enqueue_enabled=(
            app_settings.process.background_backend == "postgres"
        ),
    )
    character_ttl_reaper = CharacterTtlReaper(
        character_repository=character_repository,
        character_service=character_service,
        account_runtime_profile_resolver=account_runtime_profile_resolver,
        account_runtime_usage_repository=account_runtime_usage_repository,
        clock=clock,
    )
    proactive_scheduler.set_character_ttl_reaper(character_ttl_reaper)
    # Idle-character auto-freeze sweep (CHARACTER_FREEZE_PLAN). Reads the
    # ``character_freeze`` site-settings group each sweep and freezes
    # characters idle past the configured threshold. No-op until an
    # operator enables auto-freeze in the admin console.
    from kokoro_link.application.services.app_runtime_settings_service import (
        AppRuntimeSettingsService as _AppRuntimeSettingsService,
    )
    from kokoro_link.application.services.character_freeze_reaper import (
        CharacterFreezeReaper as _CharacterFreezeReaper,
    )

    character_freeze_reaper = _CharacterFreezeReaper(
        character_repository=character_repository,
        settings_service=_AppRuntimeSettingsService(runtime_settings_repository),
        clock=clock,
    )
    proactive_scheduler.set_character_freeze_reaper(character_freeze_reaper)
    # Cloud→Core subscription sync (invoked by the internal route on tenant
    # tier changes). Persists authoritative tenant state first, then fans out
    # through ``cloud_tenant_id`` to converge character scan projections.
    from kokoro_link.application.services.subscription_freeze_service import (
        SubscriptionFreezeService as _SubscriptionFreezeService,
    )

    subscription_freeze_service = _SubscriptionFreezeService(
        character_repository=character_repository,
        operator_profile_repository=operator_profile_repository,
        subscription_repository=cloud_subscription_repository,
        clock=clock,
    )
    # Cloud→Core per-card exclusive-freeze (invoked by the internal route on
    # an official card's contract start/end). Wired unconditionally, same as
    # ``subscription_freeze_service`` above — the internal route's own
    # credential gate already 503s a self-host deployment that never
    # configures ``KOKORO_CLOUD_INTERNAL_CREDENTIALS`` / ``_TOKENS``.
    from kokoro_link.application.services.exclusive_card_freeze_service import (
        ExclusiveCardFreezeService as _ExclusiveCardFreezeService,
    )

    exclusive_card_freeze_service = _ExclusiveCardFreezeService(
        character_repository=character_repository,
        clock=clock,
    )
    # Cloud→Core tenant-tier push (invoked by the internal route). Only wired
    # in cloud mode; self-host has no tenants to re-tier and the route 503s
    # via the token gate anyway.
    cloud_tenant_tier_sync_service = None
    if app_settings.cloud.active:
        from kokoro_link.application.services.cloud_tenant_tier_sync_service import (
            CloudTenantTierSyncService as _CloudTenantTierSyncService,
        )

        cloud_tenant_tier_sync_service = _CloudTenantTierSyncService(
            operator_profile_repository=operator_profile_repository,
        )
    character_runtime_initializer = CharacterRuntimeInitializer(
        character_service=character_service,
        schedule_service=schedule_service,
        story_arc_service=story_arc_service,
        story_event_service=story_event_service,
    )
    character_primary_image_initializer = CharacterPrimaryImageInitializer(
        character_service=character_service,
        character_image_service=character_image_service,
    )
    chat_assist_service = ChatAssistService(
        character_service=character_service,
        active_llm_provider=active_llm_provider,
        conversation_repository=conversation_repository,
        schedule_service=schedule_service,
        story_arc_repository=story_arc_repository,
        world_event_repository=world_event_repository,
        operator_profile_service=operator_profile_service,
        subscription_access_guard=subscription_access_guard,
        cloud_mode=app_settings.cloud.active,
        player_persona_note_repository=player_persona_note_repository,
    )
    if operator_persona_service is not None:
        operator_persona_projection_service = OperatorPersonaProjectionService(
            character_service=character_service,
            persona_service=operator_persona_service,
            active_llm_provider=active_llm_provider,
            operator_profile_service=operator_profile_service,
        )
    # Fusion-story service is its own auxiliary pipeline. Built here
    # because it needs ``character_service`` (for entity lookup) and the
    # already-built ``active_llm_provider`` + ``memory_repository``.
    fusion_story_service = FusionStoryService(
        repository=fusion_story_repository,
        character_service=character_service,
        brief_builder=FusionCharacterBriefBuilder(
            memory_repository=memory_repository,
        ),
        planner=FusionStoryPlanner(
            provider=active_llm_provider, feature_key=FEATURE_FUSION_STORY,
        ),
        writer=FusionStoryWriter(
            provider=active_llm_provider, feature_key=FEATURE_FUSION_STORY,
        ),
        polisher=FusionStoryPolisher(
            provider=active_llm_provider, feature_key=FEATURE_FUSION_STORY,
        ),
        critic=FusionStoryCritic(
            provider=active_llm_provider,
            feature_key=FEATURE_FUSION_STORY_CRITIC,
        ),
        jobs=studio_job_repository,
        notifications=notification_service,
        execution_lease=studio_execution_lease,
        action_billing=action_billing_service,
        activity_anchor=character_activity_anchor,
    )
    # Fusion material-richness stats (Creator Studio C1-P1). Shares
    # ``select_brief_memories`` with the brief builder above so the picker
    # badge reflects the exact memory slice a fusion story would pull;
    # reads its tier thresholds from the ``fusion_material`` site-settings
    # group (admin-configurable, DB-only).
    fusion_material_stats_service = FusionMaterialStatsService(
        memory_repository=memory_repository,
        settings_service=_AppRuntimeSettingsService(
            runtime_settings_repository,
        ),
    )
    fusion_to_arc_draft_service = FusionToArcDraftService(
        fusion_story_service=fusion_story_service,
        character_service=character_service,
        adapter=LLMFusionToArcAdapter(
            provider=active_llm_provider,
            feature_key=FEATURE_ARC_ADAPT,
        ),
        # OP1-C — when the creator asks to be written into (or to watch)
        # the story, the adaptation needs to know who they are to this
        # cast: how each character addresses them, how close they stand.
        # Never read under 「保持原樣」, where the player is not in the
        # story at all. Fail-soft: no seed renders the mode block alone.
        relationship_seed_repository=relationship_seed_repository,
        operator_profile_service=operator_profile_service,
    )
    arc_series_continuation_draft_service = ArcSeriesContinuationDraftService(
        series_repository=arc_series_repository,
        character_repository=character_repository,
        story_arc_repository=story_arc_repository,
        story_event_repository=story_event_repository,
        memory_repository=memory_repository,
        adapter=LLMArcSeriesContinuationDraftAdapter(
            provider=active_llm_provider,
            feature_key=FEATURE_ARC_CONTINUATION_DRAFT,
            # GF6 — hosted tone policy (domain/services/story_tone_policy).
            cloud_mode=app_settings.cloud.active,
        ),
    )

    # Character card export — projects A-layer settings + bundled arc
    # templates + stage images into a portable ``.lumecard``. Built here
    # because it needs the already-wired ``character_service`` (entity
    # lookup), ``object_storage`` (stage-image bytes), and
    # ``arc_template_repository`` (template serialisation).
    character_card_export_service = CharacterCardExportService(
        character_service=character_service,
        object_storage=object_storage,
        arc_template_repository=arc_template_repository,
        arc_series_repository=arc_series_repository,
    )

    # Full character backup export (CB2) — DB-backed only (see the repo /
    # reader wiring above). Reuses the arc repositories the `.lumecard`
    # exporter uses so bundled templates behave identically, and the
    # account-runtime-usage ledger for the hosted 24h throttle.
    #
    # Both backup services share ONE per-job execution lease (A1 — the
    # studio-lease precedent this line originally skipped): scaled api
    # replicas all run startup recovery, and without a claim a restarting
    # replica's recover() tears down the rows a live replica is still
    # landing (restore) or double-drives a dump (export). Same backend
    # selection as the studio lease; a self-host single process gets the
    # in-process backend, byte-identical to the historical behaviour.
    character_backup_execution_lease = None
    character_backup_export_service: CharacterBackupExportService | None = None
    if (
        character_backup_job_repository is not None
        and character_backup_export_reader is not None
    ):
        from kokoro_link.application.services.studio_execution_lease import (
            StudioExecutionLease,
        )
        from kokoro_link.infrastructure.build_info import get_build_info

        character_backup_execution_lease = StudioExecutionLease(
            _build_runtime_lease_backend(app_settings, db_session_factory),
            owner_id=_runtime_lease_owner_id("backup"),
            name_prefix="backup:",
        )
        character_backup_export_service = CharacterBackupExportService(
            job_repository=character_backup_job_repository,
            export_reader=character_backup_export_reader,
            object_storage=object_storage,
            arc_template_repository=arc_template_repository,
            arc_series_repository=arc_series_repository,
            account_runtime_usage_repository=account_runtime_usage_repository,
            execution_lease=character_backup_execution_lease,
            cloud_mode=app_settings.cloud.active,
            app_version=get_build_info().version,
        )

    # Character card import — the mirror of export: validates the zip,
    # lands bundled arc templates (collision-remapping ids), creates the
    # character from the A-layer profile, and re-uploads stage images via
    # ``character_image_service`` so they land in the importer's storage.
    character_card_import_service = CharacterCardImportService(
        character_service=character_service,
        character_image_service=character_image_service,
        arc_template_repository=arc_template_repository,
        arc_series_repository=arc_series_repository,
        translator=LLMCharacterCardTranslator(
            provider=active_llm_provider,
            feature_key=FEATURE_CARD_TRANSLATE,
        ),
        arc_template_translator=arc_template_translator,
        cloud_mode=app_settings.cloud.active,
    )

    # Full character backup import (CB3) — DB-backed only, like the
    # export half above. The restore rides existing seams end to end:
    # the character-create gates through ``character_service``, the arc
    # template / series landing through the ``.lumecard`` import
    # service, the memory embedding recompute through the same
    # repo + embedder pair ``chat_service`` uses, and the export reader
    # for crash-recovery media reverse-lookup.
    character_backup_import_service: CharacterBackupImportService | None = None
    if (
        character_backup_job_repository is not None
        and character_backup_export_reader is not None
        and db_session_factory is not None
    ):
        from kokoro_link.infrastructure.persistence.sa_character_backup_restore_writer import (
            SACharacterBackupRestoreWriter,
        )

        character_backup_import_service = CharacterBackupImportService(
            job_repository=character_backup_job_repository,
            restore_writer=SACharacterBackupRestoreWriter(
                db_session_factory,
            ),
            export_reader=character_backup_export_reader,
            object_storage=object_storage,
            character_service=character_service,
            card_import_service=character_card_import_service,
            arc_template_repository=arc_template_repository,
            arc_series_repository=arc_series_repository,
            memory_repository=memory_repository,
            embedder=embedder,
            # A6 hosted upload throttle + A4 job-keyed create-event dedup
            # both read/write this ledger.
            account_runtime_usage_repository=account_runtime_usage_repository,
            # A1: shared with the export service — one lease table, one
            # owner per process, per-job names.
            execution_lease=character_backup_execution_lease,
            cloud_mode=app_settings.cloud.active,
        )

    # SillyTavern card front layer — converts a parsed ST V2/V3 card into
    # a ``CharacterCardManifest`` (LLM-normalising its free-text prose)
    # that the route packs into an in-memory ``.lumecard`` and feeds back
    # through the import service above, so the downstream pipeline is
    # untouched.
    sillytavern_convert_service = SillyTavernConvertService(
        normalizer=LLMSillyTavernNormalizer(
            provider=active_llm_provider,
            feature_key=FEATURE_SILLYTAVERN_NORMALIZE,
        ),
    )

    # Character card catalogue — the official cards from the Cloud catalog
    # plus any local ``.lumecard`` packs, both installed through the same
    # import path as a manual upload. Nothing here connects at boot: the
    # catalog client is lazy, so a Cloud outage cannot delay startup, and an
    # empty ``KOKORO_OFFICIAL_CARD_CATALOG_URL`` leaves the official source
    # unwired entirely (self-host opt-out, plan §3.5).
    official_card_source: OfficialCardPackSource | None = None
    if app_settings.official_cards.enabled:
        official_card_catalog_client = OfficialCardCatalogClient(
            base_url=app_settings.official_cards.catalog_url,
        )
        official_card_catalog = CachedOfficialCardCatalog(
            client=official_card_catalog_client,
        )
        # Cloud-exclusive (IP-partner) cards, EC4. The client is built only
        # when this deployment holds a User-service credential carrying the
        # exclusive-read scope — which a self-hosted one never does, and
        # that absence *is* the red line: no client, no install button, no
        # path to a partner's text. It is not an env switch anyone can flip.
        exclusive_payload_client = build_exclusive_payload_client(
            base_url=app_settings.cloud.user_service_url,
            credential_descriptor=app_settings.cloud.internal_service_credential,
        )
        exclusive_installer = (
            ExclusiveOfficialCardInstaller(
                catalog=official_card_catalog,
                exclusive_payloads=exclusive_payload_client,
                character_service=character_service,
                character_image_service=character_image_service,
                voice_catalog=tts_voice_catalog,
            )
            if exclusive_payload_client is not None
            else None
        )
        # Tier-fenced official cards (TG3). Built from the same credential
        # and the same scope as the installer above, which is why the two
        # can never disagree: a deployment able to *see* a card being
        # internally tested is exactly one able to install it. No
        # credential — every self-hosted build — means no client, and those
        # cards then do not exist here at all rather than existing as rows
        # nobody may take.
        #
        # Two URLs on purpose: the *document* is read from the internal User
        # service, while the *art* those rows point at is served by the
        # public catalog and downloaded anonymously — so the image paths are
        # absolutised by the anonymous client's own absolutiser, which is
        # also the origin its download guard checks against. Absolutising
        # them against the User service URL instead lands a card with no
        # stage images and no error anywhere.
        gated_catalog_client = (
            build_gated_catalog_client(
                base_url=app_settings.cloud.user_service_url,
                credential_descriptor=(
                    app_settings.cloud.internal_service_credential
                ),
                absolutise_asset=official_card_catalog_client.absolutise,
            )
            if exclusive_payload_client is not None
            else None
        )
        official_card_source = OfficialCardPackSource(
            catalog=official_card_catalog,
            import_service=character_card_import_service,
            exclusive_installer=exclusive_installer,
            gated_catalog=gated_catalog_client,
        )
    character_card_pack_service = CharacterCardPackService(
        catalog=CharacterCardPackCatalog(),
        import_service=character_card_import_service,
        official_cards=official_card_source,
    )

    scene_image_port = _build_scene_image_port(
        settings=app_settings,
        active_image_provider=active_image_provider,
    )

    branching_drama_service = BranchingDramaService(
        repository=branching_drama_repository,
        character_service=character_service,
        brief_builder=FusionCharacterBriefBuilder(
            memory_repository=memory_repository,
        ),
        planner=BranchingDramaPlanner(
            provider=active_llm_provider,
            feature_key=FEATURE_BRANCHING_DRAMA,
        ),
        director=BranchingDramaDirector(
            provider=active_llm_provider,
            feature_key=FEATURE_BRANCHING_DRAMA,
        ),
        critic=BranchingDramaCritic(
            provider=active_llm_provider,
            feature_key=FEATURE_BRANCHING_DRAMA_CRITIC,
        ),
        polisher=BranchingDramaPolisher(
            provider=active_llm_provider,
            feature_key=FEATURE_BRANCHING_DRAMA_CRITIC,
        ),
        uploads_dir=app_settings.uploads_dir,
        scene_image=scene_image_port,
        object_storage=object_storage,
        event_seed_dispenser=event_seed_dispenser,
        visual_style_service=visual_generation_style_service,
        jobs=studio_job_repository,
        execution_lease=studio_execution_lease,
        image_prefetch_depth=_drama_image_prefetch_depth(),
        action_billing=action_billing_service,
        activity_anchor=character_activity_anchor,
    )

    # BD7 — the exit from a finished playthrough: the line the player
    # walked becomes an unsaved arc-template draft the創作專區 wizard
    # picks up. Same ``arc_adapt`` feature key as the fusion conversion
    # (converting a finished work has one price), and the same fail-soft
    # relationship material for the two modes that put the player in it.
    drama_to_arc_draft_service = DramaToArcDraftService(
        drama_service=branching_drama_service,
        character_service=character_service,
        adapter=LLMDramaToArcAdapter(
            provider=active_llm_provider,
            feature_key=FEATURE_ARC_ADAPT,
        ),
        relationship_seed_repository=relationship_seed_repository,
        operator_profile_service=operator_profile_service,
    )

    # Startup recovery for interrupted Creator Studio pipelines —
    # invoked once from the FastAPI lifespan (fail-soft there). Shares the
    # execution lease so a scaled api replica skips targets another replica is
    # already recovering, and hands a re-driven target's lease to the runner.
    studio_job_recovery_service = StudioJobRecoveryService(
        jobs=studio_job_repository,
        fusion_story_service=fusion_story_service,
        branching_drama_service=branching_drama_service,
        execution_lease=studio_execution_lease,
    )

    return ServiceContainer(
        character_service=character_service,
        chat_service=chat_service,
        goal_service=goal_service,
        schedule_service=schedule_service,
        character_draft_service=character_draft_service,
        character_creation_intake_service=character_creation_intake_service,
        companion_draft_service=companion_draft_service,
        character_image_service=character_image_service,
        character_lora_service=character_lora_service,
        character_relationship_service=character_relationship_service,
        character_encounter_service=character_encounter_service,
        album_service=album_service,
        tool_registry=tool_registry,
        tool_orchestrator=tool_orchestrator,
        tool_invocation_repository=tool_invocation_repository,
        memory_repository=memory_repository,
        memory_admin_service=memory_admin_service,
        memory_consolidation_service=memory_consolidation_service,
        state_history_repository=state_history_repository,
        embedder=embedder,
        object_storage=object_storage,
        provider_ids=model_registry.list_ids(),
        model_registry=model_registry,
        image_profile_registry=image_profile_registry,
        video_profile_registry=video_profile_registry,
        preferences_repository=preferences_repository,
        schedule_memorializer=schedule_memorializer,
        schedule_weather_drift_service=schedule_weather_drift_service,
        active_llm_provider=active_llm_provider,
        cloud_routing_profile_resolver=cloud_routing_profile_resolver,
        cloud_mode=app_settings.cloud.active,
        nsfw_mode_service=nsfw_mode_service,
        visual_generation_style_service=visual_generation_style_service,
        conversation_repository=conversation_repository,
        operator_profile_repository=operator_profile_repository,
        operator_profile_service=operator_profile_service,
        geo_location_provider=geo_location_provider,
        site_settings_holder=site_settings_holder,
        site_settings_reloader=site_settings_reloader,
        auth_service=auth_service,
        auth_strategy=auth_strategy,
        password_hasher=password_hasher,
        jwt_service=jwt_service,
        messaging_dispatcher=messaging_dispatcher,
        telegram_polling_service=telegram_polling_service,
        discord_gateway_service=discord_gateway_service,
        whatsapp_gateway_service=whatsapp_gateway_service,
        messaging_account_service=messaging_account_service,
        channel_binding_service=channel_binding_service,
        web_push_subscription_repository=web_push_subscription_repository,
        notification_preferences_repository=notification_preferences_repository,
        web_push_sender=web_push_sender,
        notification_service=notification_service,
        proactive_attempt_repository=proactive_attempt_repository,
        proactive_dispatcher=proactive_dispatcher,
        proactive_scheduler=proactive_scheduler,
        character_tick_executor=proactive_scheduler.character_tick_executor,
        social_tick_executor=proactive_scheduler.social_tick_executor,
        scheduler_metrics=scheduler_metrics,
        drain_state=drain_state,
        background_shadow_coordinator=background_shadow_coordinator,
        background_shadow_worker=background_shadow_worker,
        background_job_queue=background_job_queue,
        background_coordinator_lease=background_coordinator_lease,
        runtime_ownership=runtime_ownership,
        execution_mode_transition=execution_mode_transition,
        character_ttl_reaper=character_ttl_reaper,
        character_freeze_reaper=character_freeze_reaper,
        subscription_freeze_service=subscription_freeze_service,
        exclusive_card_freeze_service=exclusive_card_freeze_service,
        cloud_tenant_tier_sync_service=cloud_tenant_tier_sync_service,
        subscription_access_guard=subscription_access_guard,
        cloud_subscription_repository=cloud_subscription_repository,
        cloud_credit_service=cloud_credit_service,
        cloud_pricing_service=cloud_pricing_service,
        cloud_announcement_service=cloud_announcement_service,
        action_billing_service=action_billing_service,
        cloud_tier_pricing_service=cloud_tier_pricing_service,
        quota_overage_service=quota_overage_service,
        player_runtime_limits_service=player_runtime_limits_service,
        player_locale_service=player_locale_service,
        geocoding_client=geocoding_client,
        external_chat_roster_service=external_chat_roster_service,
        external_chat_attachment_service=external_chat_attachment_service,
        external_chat_turn_service=external_chat_turn_service,
        character_repository=character_repository,
        character_activity_stats_service=character_activity_stats_service,
        proactive_event_bus=proactive_event_bus,
        story_seed_repository=story_seed_repository,
        story_event_repository=story_event_repository,
        story_event_service=story_event_service,
        story_beat_scene_service=story_beat_scene_service,
        story_arc_repository=story_arc_repository,
        story_arc_service=story_arc_service,
        story_scene_service=story_scene_service,
        story_scene_session_repository=story_scene_session_repository,
        arc_template_repository=arc_template_repository,
        arc_template_translator=arc_template_translator,
        arc_template_intake_service=arc_template_intake_service,
        arc_template_pack_sync_service=arc_template_pack_sync_service,
        arc_series_repository=arc_series_repository,
        arc_series_service=arc_series_service,
        arc_series_continuation_draft_service=arc_series_continuation_draft_service,
        character_card_export_service=character_card_export_service,
        character_card_import_service=character_card_import_service,
        sillytavern_convert_service=sillytavern_convert_service,
        character_card_pack_service=character_card_pack_service,
        character_primary_image_initializer=character_primary_image_initializer,
        character_runtime_initializer=character_runtime_initializer,
        chat_assist_service=chat_assist_service,
        turn_journal_repository=turn_journal_repository,
        turn_undo_service=turn_undo_service,
        feed_post_repository=feed_post_repository,
        feed_reaction_repository=feed_reaction_repository,
        feed_reaction_service=feed_reaction_service,
        feed_comment_repository=feed_comment_repository,
        feed_comment_service=feed_comment_service,
        feed_composer_service=feed_composer_service,
        feed_video_job_service=feed_video_job_service,
        pending_feed_video_repository=pending_feed_video_repository,
        feed_comment_reply_service=feed_comment_reply_service,
        feed_reaction_memorializer=feed_reaction_memorializer,
        feed_event_bus=feed_event_bus,
        realtime_outbox=_realtime.outbox,
        realtime_rehydrator=_realtime.rehydrator,
        realtime_dispatcher=_realtime.dispatcher,
        tts_service=tts_service,
        tts_pregeneration_service=tts_pregeneration_service,
        tts_voice_catalog=tts_voice_catalog,
        fusion_story_repository=fusion_story_repository,
        fusion_story_service=fusion_story_service,
        fusion_material_stats_service=fusion_material_stats_service,
        fusion_to_arc_draft_service=fusion_to_arc_draft_service,
        branching_drama_service=branching_drama_service,
        drama_to_arc_draft_service=drama_to_arc_draft_service,
        studio_job_repository=studio_job_repository,
        studio_job_recovery_service=studio_job_recovery_service,
        character_backup_job_repository=character_backup_job_repository,
        character_backup_export_service=character_backup_export_service,
        character_backup_import_service=character_backup_import_service,
        world_event_repository=world_event_repository,
        rss_source_repository=rss_source_repository,
        character_event_inbox_repository=character_event_inbox_repository,
        rss_ingestion_service=rss_ingestion_service,
        event_curator_service=event_curator_service,
        event_seed_dispenser=event_seed_dispenser,
        world_event_scheduler=world_event_scheduler,
        rss_source_sync_service=rss_source_sync_service,
        pending_follow_up_repository=pending_follow_up_repository,
        pending_follow_up_dispatcher=pending_follow_up_dispatcher,
        operator_persona_service=operator_persona_service,
        operator_persona_projection_service=operator_persona_projection_service,
        relationship_seed_repository=relationship_seed_repository,
        address_change_log_repository=address_change_log_repository,
        relationship_names_service=relationship_names_service,
        player_persona_note_repository=player_persona_note_repository,
        player_persona_note_service=player_persona_note_service,
        persona_extraction_service=persona_extraction_service,
        persona_dream_service=persona_dream_service,
        persona_curiosity_service=persona_curiosity_service,
        persona_curiosity_planner=persona_curiosity_planner,
        character_relationship_repository=character_relationship_repository,
        character_peer_profile_repository=character_peer_profile_repository,
        character_social_knowledge_service=character_social_knowledge_service,
        character_encounter_repository=character_encounter_repository,
        character_encounter_intent_repository=character_encounter_intent_repository,
        album_repository=album_repository,
        turn_record_repository=turn_record_repository,
        usage_event_repository=usage_event_repository,
        emotion_event_repository=emotion_event_repository,
        disposition_drift_history_repository=disposition_drift_history_repository,
        self_reflection_repository=self_reflection_repository,
        memoir_pin_repository=memoir_pin_repository,
        memoir_service=memoir_service,
        behavioral_pattern_repository=behavioral_pattern_repository,
        deferred_intent_repository=deferred_intent_repository,
        runtime_settings_repository=runtime_settings_repository,
        provider_connection_repository=provider_connection_repository,
        provider_connection_service=provider_connection_service,
        quiet_hours_service=quiet_hours_service,
        address_preference_repository=address_preference_repository,
        address_preference_service=address_preference_service,
        experiment_repository=experiment_repository,
        experiment_assignment_repository=experiment_assignment_repository,
        experiment_service=experiment_service,
        experiment_overlay_service=experiment_overlay_service,
        experiment_analysis_service=experiment_analysis_service,
        llm_priority_gate=llm_priority_gate,
        app_settings=app_settings,
        clock=clock,
        db_engine=db_engine,
    )
