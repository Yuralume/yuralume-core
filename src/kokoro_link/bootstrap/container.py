import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone, tzinfo
from urllib.parse import unquote, urlparse

_LOGGER = logging.getLogger(__name__)
from kokoro_link.application.services.album_service import AlbumService
from kokoro_link.application.services.account_runtime_profile import (
    AccountRuntimeProfileResolver,
)
from kokoro_link.application.services.auto_consolidation_trigger import (
    AutoConsolidationTrigger,
)
from kokoro_link.application.services.channel_binding_service import ChannelBindingService
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
    FEATURE_SCENE_ACCESS,
    FEATURE_DIALOGUE_SUMMARY,
    FEATURE_FEED_COMMENT_REPLY,
    FEATURE_IDLE_DRIFT,
    FEATURE_FEED_COMPOSE,
    FEATURE_BRANCHING_DRAMA,
    FEATURE_BRANCHING_DRAMA_CRITIC,
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
    FEATURE_STORY_EXPAND,
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
from kokoro_link.application.services.character_card_import_service import (
    CharacterCardImportService,
)
from kokoro_link.application.services.character_card_pack_service import (
    CharacterCardPackService,
)
from kokoro_link.infrastructure.character_card.pack_catalog import (
    CharacterCardPackCatalog,
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
from kokoro_link.infrastructure.cloud.tier_runtime_profile_client import (
    TierRuntimeProfileClient,
)
from kokoro_link.application.services.cloud_tier_profile_cache import (
    CachedTierRuntimeProfileResolver,
)
from kokoro_link.contracts.cloud_tier_runtime_profile import (
    TierRuntimeProfilePort,
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
from kokoro_link.application.services.demo_account_reaper import DemoAccountReaper
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
from kokoro_link.application.services.story_event_service import StoryEventService
from kokoro_link.application.services.story_gacha import StoryGachaService
from kokoro_link.application.services.schedule_memorializer import ScheduleMemorializer
from kokoro_link.application.services.schedule_service import ScheduleService
from kokoro_link.application.services.scene_access_service import SceneAccessService
from kokoro_link.application.services.state_tracker import StateChangeTracker
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
from kokoro_link.contracts.studio_jobs import StudioJobRepositoryPort
from kokoro_link.contracts.story_arc import (
    StoryArcPlannerPort,
    StoryArcRepositoryPort,
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
from kokoro_link.infrastructure.scene_access.llm_judge import LLMSceneAccessJudge
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
from kokoro_link.contracts.active_video import ActiveVideoProviderPort
from kokoro_link.contracts.image_profile import (
    ExternalImageApiProfileConfig,
)
from kokoro_link.contracts.video_profile import (
    ExternalVideoApiProfileConfig,
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
    TavilyClient,
    WebSearchTool,
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
from kokoro_link.infrastructure.calendar.holidays_provider import (
    HolidaysCalendarProvider,
    NullCalendarProvider,
)
from kokoro_link.infrastructure.geo.ip_api_provider import IpApiGeoLocationProvider
from kokoro_link.infrastructure.geo.null_provider import NullGeoLocationProvider
from kokoro_link.infrastructure.weather.open_meteo_provider import (
    NullWeatherProvider,
    OpenMeteoWeatherProvider,
)
from kokoro_link.infrastructure.localization.fallback_texts import (
    localized_fallback_text,
)
from kokoro_link.infrastructure.schedule.llm_planner import LLMSchedulePlanner
from kokoro_link.infrastructure.schedule.null_planner import NullSchedulePlanner
from kokoro_link.infrastructure.schedule.stub_planner import StubSchedulePlanner
from kokoro_link.infrastructure.state.simple import SimpleStateEngine
from kokoro_link.infrastructure.storage.http import HttpObjectStorage
from kokoro_link.infrastructure.storage.in_memory import InMemoryObjectStorage
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
    active_llm_provider: ActiveLLMProviderPort | None = None
    cloud_routing_profile_resolver: CloudRoutingProfilePort | None = None
    nsfw_mode_service: NsfwModeService | None = None
    visual_generation_style_service: VisualGenerationStyleService | None = None
    object_storage: ObjectStoragePort = field(
        default_factory=lambda: InMemoryObjectStorage(),
    )
    scene_access_service: SceneAccessService | None = None
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
    auth_service: "AuthService | None" = None
    auth_strategy: "AuthStrategy | None" = None
    password_hasher: "PasswordHasherPort | None" = None
    jwt_service: "JWTService | None" = None
    story_seed_repository: StorySeedRepositoryPort | None = None
    story_event_repository: StoryEventRepositoryPort | None = None
    story_event_service: StoryEventService | None = None
    story_beat_scene_service: StoryBeatSceneService | None = None
    story_arc_repository: StoryArcRepositoryPort | None = None
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
    demo_account_reaper: DemoAccountReaper | None = None
    character_freeze_reaper: "CharacterFreezeReaper | None" = None
    # Cloud→Core subscription-lapse batch freeze/thaw, invoked by the
    # internal service-to-service route on tenant tier changes.
    subscription_freeze_service: "SubscriptionFreezeService | None" = None
    # Cloud→Core tenant-tier push, invoked by the internal route so a tier
    # change takes effect without waiting for the operator to re-login.
    cloud_tenant_tier_sync_service: "CloudTenantTierSyncService | None" = None
    subscription_access_guard: "SubscriptionAccessGuard | None" = None
    cloud_subscription_repository: "CloudSubscriptionRepositoryPort | None" = None
    # Exposed for the admin character-freeze surface (site-wide overview
    # + immediate freeze/unfreeze) which needs list / get / set_frozen.
    character_repository: "CharacterRepositoryPort | None" = None
    proactive_event_bus: ProactiveEventBus | None = None
    feed_post_repository: FeedPostRepositoryPort | None = None
    feed_reaction_repository: FeedReactionRepositoryPort | None = None
    feed_reaction_service: "FeedReactionService | None" = None
    feed_comment_repository: FeedCommentRepositoryPort | None = None
    feed_comment_service: "FeedCommentService | None" = None
    feed_composer_service: FeedComposerService | None = None
    feed_comment_reply_service: FeedCommentReplyService | None = None
    feed_reaction_memorializer: FeedReactionMemorializer | None = None
    feed_event_bus: FeedEventBus | None = None
    tts_service: TTSService | None = None
    tts_pregeneration_service: TTSPregenerationService | None = None
    tts_voice_catalog: TTSVoiceCatalogPort | None = None
    fusion_story_repository: FusionStoryRepositoryPort | None = None
    fusion_story_service: FusionStoryService | None = None
    fusion_material_stats_service: FusionMaterialStatsService | None = None
    fusion_to_arc_draft_service: FusionToArcDraftService | None = None
    branching_drama_service: "BranchingDramaService | None" = None
    studio_job_repository: StudioJobRepositoryPort | None = None
    studio_job_recovery_service: StudioJobRecoveryService | None = None
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
    # docs/MEMOIR_PLAN.md — player-side memoir aggregation.
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
    # ADDRESS_RESOLVER_PLAN §4–§8 — per-pair rename log + names edit.
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


def _build_db_repositories(database_url: str) -> _RepoBundle:
    from kokoro_link.infrastructure.persistence.engine import (
        build_async_engine,
        build_session_factory,
    )
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

    engine = build_async_engine(database_url)
    session_factory = build_session_factory(engine)
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
) -> GoalReviewerPort:
    if active_provider is None:
        return NullGoalReviewer()
    return LLMGoalReviewer(
        provider=active_provider, feature_key=FEATURE_GOAL_REVIEW,
    )


def _build_story_expander(
    *,
    registry: ChatModelRegistryPort,
    default_provider_id: str,
    active_provider: ActiveLLMProviderPort | None = None,
):
    """Pick an expander based on available providers.

    Fake-provider deployments get ``NullStoryEventExpander`` (plain
    single-sentence narrative). Real providers get the LLM-backed
    expander wired to the UI-selected model.
    """
    if active_provider is None:
        return NullStoryEventExpander()
    return LLMStoryEventExpander(
        provider=active_provider, feature_key=FEATURE_STORY_EXPAND,
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


def _build_embedder(*, settings: AppSettings) -> EmbedderPort:
    """Install a stable embedder reference with a runtime-switchable backend."""
    if settings.use_embedder:
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


def _build_calendar_provider(
    *, settings: AppSettings, local_tz: tzinfo,
) -> CalendarContextPort:
    """Build the calendar-facts provider.

    Returns the null implementation when calendar context is disabled
    via ``KOKORO_CALENDAR_ENABLED=false`` so prompt blocks stay empty;
    otherwise instantiates the ``holidays``-backed adapter for the
    operator-configured region (default ``TW``).
    """
    if not settings.calendar.enabled:
        return NullCalendarProvider()
    return HolidaysCalendarProvider(
        region=settings.calendar.region, local_tz=local_tz,
    )


def _build_weather_provider(*, settings: AppSettings):
    """Build the weather-facts provider.

    Falls through to :class:`NullWeatherProvider` when weather is
    disabled or lat/lon is missing — prompt blocks stay empty without
    any conditional logic at the call sites (mirrors the calendar
    provider's "always installable" contract)."""
    weather = settings.weather
    if not weather.enabled:
        return NullWeatherProvider()
    # When the deployment leaves the label empty, resolve it to the deploy-time
    # content language instead of the provider's hardcoded Chinese last resort,
    # so an en/ja deployment's fallback weather label follows its own language.
    location_label = weather.location_label.strip() or localized_fallback_text(
        "weather.current_location_label", settings.default_primary_language,
    )
    return OpenMeteoWeatherProvider(
        latitude=weather.latitude,
        longitude=weather.longitude,
        location_label=location_label,
        timezone_id=weather.timezone_id,
        cache_ttl_seconds=weather.cache_ttl_seconds,
    )


def _build_geo_location_provider(*, settings: AppSettings) -> GeoLocationPort:
    geoip = settings.geoip
    if not geoip.enabled:
        return NullGeoLocationProvider()
    if geoip.provider != "ip-api":
        return NullGeoLocationProvider()
    return IpApiGeoLocationProvider(
        endpoint=geoip.endpoint,
        timeout_seconds=geoip.timeout_seconds,
        cache_ttl_seconds=geoip.cache_ttl_seconds,
    )


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


def _build_tool_registry(
    *,
    settings: AppSettings,
    image_provider: ActiveImageProviderPort,
    object_storage: ObjectStoragePort,
    album_service: AlbumService | None = None,
    visual_style_service: VisualGenerationStyleService | None = None,
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


def build_container(settings: AppSettings | None = None) -> ServiceContainer:
    app_settings = settings or AppSettings.from_env()
    # Site-level runtime settings (Weather/Calendar/GeoIP/NSFW/world-event
    # policy) are DB-authoritative once seeded (CORE_ENV_TO_ADMIN_CONFIG
    # track 2). Read them here, before any provider is wired, and overlay
    # onto app_settings so the whole downstream wiring receives the
    # DB-effective values; env stays the fallback + first-boot seed.
    # Fail-soft: any read error keeps the env-derived settings.
    if app_settings.use_database:
        from kokoro_link.bootstrap.app_runtime_settings_seed import (
            overlay_site_settings_from_db,
        )

        app_settings = overlay_site_settings_from_db(app_settings)
    clock = SystemClock()
    object_storage = _build_object_storage(app_settings)

    preferences_repository: PreferencesRepositoryPort
    if app_settings.use_database:
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
        ) = _build_db_repositories(app_settings.database_url)
        from kokoro_link.infrastructure.persistence.engine import (
            build_async_engine,
            build_session_factory,
        )
        from kokoro_link.infrastructure.persistence.sa_preferences_repository import (
            SAPreferencesRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_operator_profile_repository import (
            SAOperatorProfileRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_initial_relationship_repository import (
            SACharacterOperatorRelationshipSeedRepository,
        )
        # Reuse a dedicated session factory for the prefs repo. Building
        # its own engine is cheap and keeps the main ``_build_db_repositories``
        # tuple unchanged — no ripple through every test harness.
        _prefs_engine = build_async_engine(app_settings.database_url)
        _prefs_session_factory = build_session_factory(_prefs_engine)
        preferences_repository = SAPreferencesRepository(_prefs_session_factory)
        operator_profile_repository: OperatorProfileRepositoryPort = (
            SAOperatorProfileRepository(_prefs_session_factory)
        )
        from kokoro_link.infrastructure.persistence.sa_cloud_subscription_repository import (
            SACloudSubscriptionRepository,
        )
        cloud_subscription_repository = SACloudSubscriptionRepository(
            _prefs_session_factory,
        )
        relationship_seed_repository = (
            SACharacterOperatorRelationshipSeedRepository(_prefs_session_factory)
        )
        from kokoro_link.infrastructure.persistence.sa_notifications import (
            SaNotificationPreferencesRepository,
            SaWebPushSubscriptionRepository,
        )
        web_push_subscription_repository = SaWebPushSubscriptionRepository(
            _prefs_session_factory,
        )
        notification_preferences_repository = (
            SaNotificationPreferencesRepository(_prefs_session_factory)
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
    if app_settings.use_database:
        from kokoro_link.infrastructure.persistence.engine import (
            build_async_engine as _fs_build_engine,
            build_session_factory as _fs_build_session_factory,
        )
        from kokoro_link.infrastructure.persistence.sa_fusion_story_repository import (
            SAFusionStoryRepository,
        )
        _fs_engine = _fs_build_engine(app_settings.database_url)
        _fs_session_factory = _fs_build_session_factory(_fs_engine)
        fusion_story_repository = SAFusionStoryRepository(_fs_session_factory)
    else:
        fusion_story_repository = InMemoryFusionStoryRepository()

    branching_drama_repository: BranchingDramaRepositoryPort
    if app_settings.use_database:
        from kokoro_link.infrastructure.persistence.engine import (
            build_async_engine as _bd_build_engine,
            build_session_factory as _bd_build_session_factory,
        )
        from kokoro_link.infrastructure.persistence.sa_branching_drama_repository import (
            SABranchingDramaRepository,
        )
        _bd_engine = _bd_build_engine(app_settings.database_url)
        _bd_session_factory = _bd_build_session_factory(_bd_engine)
        branching_drama_repository = SABranchingDramaRepository(_bd_session_factory)
    else:
        from kokoro_link.infrastructure.repositories.in_memory_branching_drama import (
            InMemoryBranchingDramaRepository,
        )
        branching_drama_repository = InMemoryBranchingDramaRepository()

    # Creator Studio durable job ledger (C0) — same isolated-engine
    # pattern as fusion_story / branching_drama above.
    studio_job_repository: StudioJobRepositoryPort
    if app_settings.use_database:
        from kokoro_link.infrastructure.persistence.engine import (
            build_async_engine as _sj_build_engine,
            build_session_factory as _sj_build_session_factory,
        )
        from kokoro_link.infrastructure.persistence.sa_studio_job_repository import (
            SAStudioJobRepository,
        )
        _sj_engine = _sj_build_engine(app_settings.database_url)
        _sj_session_factory = _sj_build_session_factory(_sj_engine)
        studio_job_repository = SAStudioJobRepository(_sj_session_factory)
    else:
        from kokoro_link.infrastructure.repositories.in_memory_studio_jobs import (
            InMemoryStudioJobRepository,
        )
        studio_job_repository = InMemoryStudioJobRepository()

    # Busy-defer follow-up repo. Same isolated-engine pattern as
    # fusion_story / branching_drama — additions never ripple through
    # the main ``_RepoBundle``.
    pending_follow_up_repository: "PendingFollowUpRepositoryPort"
    if app_settings.use_database:
        from kokoro_link.infrastructure.persistence.engine import (
            build_async_engine as _pf_build_engine,
            build_session_factory as _pf_build_session_factory,
        )
        from kokoro_link.infrastructure.persistence.sa_pending_follow_up_repository import (
            SaPendingFollowUpRepository,
        )
        _pf_engine = _pf_build_engine(app_settings.database_url)
        _pf_session_factory = _pf_build_session_factory(_pf_engine)
        pending_follow_up_repository = SaPendingFollowUpRepository(
            _pf_session_factory,
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
    if app_settings.use_database:
        from kokoro_link.infrastructure.persistence.engine import (
            build_async_engine as _we_build_engine,
            build_session_factory as _we_build_session_factory,
        )
        from kokoro_link.infrastructure.persistence.sa_world_event_repository import (
            SaWorldEventRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_rss_source_repository import (
            SaRssSourceRepository,
        )
        from kokoro_link.infrastructure.persistence.sa_character_event_inbox_repository import (
            SaCharacterEventInboxRepository,
        )
        _we_engine = _we_build_engine(app_settings.database_url)
        _we_session_factory = _we_build_session_factory(_we_engine)
        world_event_repository = SaWorldEventRepository(_we_session_factory)
        rss_source_repository = SaRssSourceRepository(_we_session_factory)
        character_event_inbox_repository = SaCharacterEventInboxRepository(
            _we_session_factory,
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
        world_event_repository = InMemoryWorldEventRepository()
        rss_source_repository = InMemoryRssSourceRepository()
        character_event_inbox_repository = InMemoryCharacterEventInboxRepository()

    character_relationship_repository: CharacterRelationshipRepositoryPort
    character_peer_profile_repository: CharacterPeerProfileRepositoryPort
    character_encounter_repository: CharacterEncounterRepositoryPort
    character_encounter_intent_repository: CharacterEncounterIntentRepositoryPort
    if app_settings.use_database:
        from kokoro_link.infrastructure.persistence.engine import (
            build_async_engine as _cr_build_engine,
            build_session_factory as _cr_build_session_factory,
        )
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

        _cr_engine = _cr_build_engine(app_settings.database_url)
        _cr_session_factory = _cr_build_session_factory(_cr_engine)
        character_relationship_repository = SACharacterRelationshipRepository(
            _cr_session_factory,
        )
        character_peer_profile_repository = SACharacterPeerProfileRepository(
            _cr_session_factory,
        )
        character_encounter_repository = SACharacterEncounterRepository(
            _cr_session_factory,
        )
        character_encounter_intent_repository = SACharacterEncounterIntentRepository(
            _cr_session_factory,
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
    if app_settings.database_url:
        from kokoro_link.infrastructure.persistence.engine import (
            build_async_engine as _at_build_engine,
            build_session_factory as _at_build_session_factory,
        )

        _at_engine = _at_build_engine(app_settings.database_url)
        _at_session_factory = _at_build_session_factory(_at_engine)
        arc_template_repository: ArcTemplateRepositoryPort = (
            SAArcTemplateRepository(_at_session_factory)
        )
        arc_series_repository: ArcSeriesRepositoryPort = (
            SAArcSeriesRepository(_at_session_factory)
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
    )

    operator_profile_service = OperatorProfileService(
        repository=operator_profile_repository,
    )
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
    account_runtime_profile_resolver = AccountRuntimeProfileResolver(
        operator_profile_repository,
        tier_profile_port=tier_runtime_profile_port,
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
        clock=clock.now,
    )
    geo_location_provider = _build_geo_location_provider(settings=app_settings)

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
        )
    else:
        active_llm_provider = PreferenceBackedActiveLLMProvider(
            registry=model_registry,
            preferences=preferences_repository,
            default_provider_id=app_settings.default_provider_id,
            nsfw_mode_service=nsfw_mode_service,
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
        persona_repository = SAOperatorPersonaRepository(_prefs_session_factory)
        persona_curiosity_repository = SAPersonaCuriosityRepository(
            _prefs_session_factory,
        )
        strength_calculator = InteractionStrengthCalculator(
            session_factory=_prefs_session_factory,
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
        template_translator=arc_template_translator,
    )
    calendar_provider = _build_calendar_provider(
        settings=app_settings, local_tz=local_tz,
    )
    weather_provider = _build_weather_provider(settings=app_settings)
    schedule_service = ScheduleService(
        repository=schedule_repository,
        planner=schedule_planner,
        local_tz=local_tz,
        conversation_repository=conversation_repository,
        dialogue_summarizer=dialogue_summarizer,
        story_arc_service=story_arc_service,
        calendar_context_port=calendar_provider,
        weather_context_port=weather_provider,
        relationship_seed_repository=relationship_seed_repository,
        operator_persona_service=operator_persona_service,
        operator_profile_service=operator_profile_service,
    )
    embedder = _build_embedder(settings=app_settings)

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
    character_image_service = CharacterImageService(
        character_repository=character_repository,
        uploads_dir=app_settings.uploads_dir,
        object_storage=object_storage,
        image_provider=active_image_provider,
        visual_style_service=visual_generation_style_service,
        account_runtime_profile_resolver=account_runtime_profile_resolver,
        subscription_access_guard=subscription_access_guard,
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
    )
    tool_orchestrator = ToolOrchestrator(
        registry=tool_registry,
        invocation_repository=tool_invocation_repository,
    )

    story_gacha = StoryGachaService(
        seed_repository=story_seed_repository,
        event_repository=story_event_repository,
    )
    # arc_template_repository, story_arc_planner, story_arc_service are
    # built earlier (before schedule_service) so the schedule planner can
    # read scene beats. The single shared YAML repo cache covers all
    # callers (story arc service + REST list endpoint + wizard).
    story_expander = _build_story_expander(
        registry=model_registry,
        default_provider_id=app_settings.default_provider_id,
        active_provider=active_llm_provider,
    )
    # Phase 2.7 — wizard backend. Stateless service, single shared
    # instance is fine. Routes through the per-feature LLM resolver
    # (FEATURE_ARC_TEMPLATE_INTAKE) so operators can pin a different
    # provider for wizard work than for runtime chat.
    arc_template_intake_service = ArcTemplateIntakeService(
        repository=arc_template_repository,
        provider=active_llm_provider,
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
    )
    story_beat_scene_service = StoryBeatSceneService(
        story_arc_service=story_arc_service,
        story_event_service=story_event_service,
        writer=LLMStoryBeatSceneWriter(
            provider=active_llm_provider,
            feature_key=FEATURE_ARC_SCENE_WRITE,
        ),
        local_tz=local_tz,
        operator_profile_service=operator_profile_service,
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
    if app_settings.use_database:
        from kokoro_link.infrastructure.persistence.engine import (
            build_async_engine as _tr_build_engine,
            build_session_factory as _tr_build_session_factory,
        )
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
        _tr_engine = _tr_build_engine(app_settings.database_url)
        _tr_session_factory = _tr_build_session_factory(_tr_engine)
        turn_record_repository = SATurnRecordRepository(_tr_session_factory)
        usage_event_repository = SAGenerationUsageRepository(_tr_session_factory)
        account_runtime_usage_repository = SAAccountRuntimeUsageRepository(
            _tr_session_factory,
        )
        emotion_event_repository = SAEmotionEventRepository(_tr_session_factory)
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
        deferred_intent_repository = SADeferredIntentRepository(_tr_session_factory)
        behavioral_pattern_repository = SABehavioralPatternRepository(
            _tr_session_factory,
        )
        self_reflection_repository = SASelfReflectionRepository(
            _tr_session_factory,
        )
        disposition_drift_history_repository = SADispositionDriftHistoryRepository(
            _tr_session_factory,
        )
        memoir_pin_repository: MemoirPinRepositoryPort = SAMemoirPinRepository(
            _tr_session_factory,
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

    # docs/MEMOIR_PLAN.md — player-side memoir view + pin store. Reuses
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
    _tr_factory_for_settings = locals().get("_tr_session_factory")
    if _tr_factory_for_settings is not None:
        from kokoro_link.infrastructure.persistence.sa_runtime_settings_repository import (
            SARuntimeSettingsRepository,
        )
        runtime_settings_repository = SARuntimeSettingsRepository(
            _tr_factory_for_settings,
        )
    else:
        runtime_settings_repository = InMemoryRuntimeSettingsRepository()
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
    _provider_settings_factory = locals().get("_prefs_session_factory")
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

    scene_access_judge = LLMSceneAccessJudge(
        provider=active_llm_provider,
        feature_key=FEATURE_SCENE_ACCESS,
    )
    scene_access_service = SceneAccessService(
        character_repository=character_repository,
        judge=scene_access_judge,
        conversation_repository=conversation_repository,
        schedule_service=schedule_service,
        memory_repository=memory_repository,
        pending_follow_up_repository=pending_follow_up_repository,
        relationship_seed_repository=relationship_seed_repository,
        operator_profile_service=operator_profile_service,
        operator_persona_service=operator_persona_service,
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
        emotion_event_repository=emotion_event_repository,
        self_reflection_repository=self_reflection_repository,
        address_preference_repository=(
            address_preference_repository
            if app_settings.humanization.address_preference_enabled
            else None
        ),
        address_change_log_repository=address_change_log_repository,
        relationship_names_service=relationship_names_service,
        experiment_overlay_service=experiment_overlay_service,
        nsfw_safe_summarizer=nsfw_safe_summarizer,
        account_runtime_profile_resolver=account_runtime_profile_resolver,
        account_runtime_usage_repository=account_runtime_usage_repository,
        event_seed_dispenser=event_seed_dispenser,
        clock=clock,
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

    # Dispatcher is always wired now — it just does nothing useful until
    # the operator creates at least one MessagingAccount via the UI.
    messaging_dispatcher = MessagingDispatcher(
        account_repository=messaging_account_repository,
        binding_repository=channel_binding_repository,
        conversation_repository=conversation_repository,
        chat_service=chat_service,
        adapters=messaging_adapters,
        debouncer=InboundDebouncer(),
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
    )
    event_curator_service = EventCuratorService(
        world_event_repository=world_event_repository,
        inbox_repository=character_event_inbox_repository,
        embedder=embedder,
        operator_persona_service=operator_persona_service,
        relationship_seed_repository=relationship_seed_repository,
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
        # alerts (SHIPPED_CONTENT_LOCALIZATION_PLAN #5 / D6-P3).
        deployment_region=app_settings.calendar.region,
    )

    proactive_event_bus = ProactiveEventBus()
    proactive_dispatcher = ProactiveDispatcher(
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        account_repository=messaging_account_repository,
        binding_repository=channel_binding_repository,
        attempt_repository=proactive_attempt_repository,
        gate=HeuristicProactiveGate(local_tz=local_tz),
        decider=proactive_decider,
        adapters=messaging_adapters,
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
    feed_event_bus = FeedEventBus()
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
    feed_composer_port = LLMFeedComposer(
        provider=active_llm_provider, feature_key=FEATURE_FEED_COMPOSE,
        video_enabled=bool(video_profile_registry.profile_ids),
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
    )
    pending_follow_up_dispatcher = PendingFollowUpDispatcher(
        repository=pending_follow_up_repository,
        composer=pending_follow_up_composer,
        proactive_dispatcher=proactive_dispatcher,
        character_repository=character_repository,
        conversation_repository=conversation_repository,
        schedule_service=schedule_service,
        dialogue_summarizer=dialogue_summarizer,
        scheduled_promise_composer=scheduled_promise_composer,
        operator_persona_service=operator_persona_service,
        operator_profile_service=operator_profile_service,
        local_tz=local_tz,
    )
    proactive_scheduler = ProactiveScheduler(
        dispatcher=proactive_dispatcher,
        character_repository=character_repository,
        rest_recovery_refresher=rest_recovery_refresher,
        beat_due_checker=beat_due_checker,
        schedule_service=schedule_service,
        feed_composer=feed_composer_service,
        feed_comment_reply=feed_comment_reply_service,
        pending_follow_up_dispatcher=pending_follow_up_dispatcher,
        character_encounter_service=character_encounter_service,
        character_social_knowledge_service=character_social_knowledge_service,
        schedule_memorializer=schedule_memorializer,
        persona_dream_service=persona_dream_service,
        persona_dream_repository=persona_repository,
        account_runtime_profile_resolver=account_runtime_profile_resolver,
        clock=clock,
        subscription_access_guard=subscription_access_guard,
    )

    tts_voice_catalog: TTSVoiceCatalogPort | None = None
    tts_settings = app_settings.tts
    if app_settings.cloud.active:
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

    character_service = CharacterService(
        character_repository,
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
    )
    demo_account_reaper = DemoAccountReaper(
        character_repository=character_repository,
        character_service=character_service,
        operator_profile_repository=operator_profile_repository,
        account_runtime_profile_resolver=account_runtime_profile_resolver,
        account_runtime_usage_repository=account_runtime_usage_repository,
        release_hook=cloud_user_service_client,
        clock=clock,
    )
    proactive_scheduler.set_demo_account_reaper(demo_account_reaper)
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
    )

    # SillyTavern card front layer — converts a parsed ST V2/V3 card into
    # a ``CharacterCardManifest`` (LLM-normalising its free-text prose)
    # that the route packs into an in-memory ``.lumecard`` and feeds back
    # through the import service above, so the downstream pipeline is
    # untouched. See ``docs/SILLYTAVERN_CARD_IMPORT_PLAN.md``.
    sillytavern_convert_service = SillyTavernConvertService(
        normalizer=LLMSillyTavernNormalizer(
            provider=active_llm_provider,
            feature_key=FEATURE_SILLYTAVERN_NORMALIZE,
        ),
    )

    # Character card marketplace (MVP) — indexes the bundled
    # ``data/character_cards/*.lumecard`` packs and installs them through
    # the same import path as a manual upload.
    character_card_pack_service = CharacterCardPackService(
        catalog=CharacterCardPackCatalog(),
        import_service=character_card_import_service,
    )

    scene_generator = _build_scene_generator(settings=app_settings)

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
        scene_generator=scene_generator,
        object_storage=object_storage,
        event_seed_dispenser=event_seed_dispenser,
        visual_style_service=visual_generation_style_service,
        jobs=studio_job_repository,
    )

    # Startup recovery for interrupted Creator Studio pipelines —
    # invoked once from the FastAPI lifespan (fail-soft there).
    studio_job_recovery_service = StudioJobRecoveryService(
        jobs=studio_job_repository,
        fusion_story_service=fusion_story_service,
        branching_drama_service=branching_drama_service,
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
        scene_access_service=scene_access_service,
        object_storage=object_storage,
        provider_ids=model_registry.list_ids(),
        model_registry=model_registry,
        image_profile_registry=image_profile_registry,
        video_profile_registry=video_profile_registry,
        preferences_repository=preferences_repository,
        schedule_memorializer=schedule_memorializer,
        active_llm_provider=active_llm_provider,
        cloud_routing_profile_resolver=cloud_routing_profile_resolver,
        nsfw_mode_service=nsfw_mode_service,
        visual_generation_style_service=visual_generation_style_service,
        conversation_repository=conversation_repository,
        operator_profile_repository=operator_profile_repository,
        operator_profile_service=operator_profile_service,
        geo_location_provider=geo_location_provider,
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
        demo_account_reaper=demo_account_reaper,
        character_freeze_reaper=character_freeze_reaper,
        subscription_freeze_service=subscription_freeze_service,
        cloud_tenant_tier_sync_service=cloud_tenant_tier_sync_service,
        subscription_access_guard=subscription_access_guard,
        cloud_subscription_repository=cloud_subscription_repository,
        character_repository=character_repository,
        proactive_event_bus=proactive_event_bus,
        story_seed_repository=story_seed_repository,
        story_event_repository=story_event_repository,
        story_event_service=story_event_service,
        story_beat_scene_service=story_beat_scene_service,
        story_arc_repository=story_arc_repository,
        story_arc_service=story_arc_service,
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
        feed_comment_reply_service=feed_comment_reply_service,
        feed_reaction_memorializer=feed_reaction_memorializer,
        feed_event_bus=feed_event_bus,
        tts_service=tts_service,
        tts_pregeneration_service=tts_pregeneration_service,
        tts_voice_catalog=tts_voice_catalog,
        fusion_story_repository=fusion_story_repository,
        fusion_story_service=fusion_story_service,
        fusion_material_stats_service=fusion_material_stats_service,
        fusion_to_arc_draft_service=fusion_to_arc_draft_service,
        branching_drama_service=branching_drama_service,
        studio_job_repository=studio_job_repository,
        studio_job_recovery_service=studio_job_recovery_service,
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
    )
