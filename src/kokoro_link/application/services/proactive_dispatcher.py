"""Proactive messaging dispatcher.

Coordinates a single evaluation pass for one character:

1. Short-circuit if the character opted out.
2. Collect cheap signals (idle time, sent-today count, last attempt
   time, current activity) and ask the gate.
3. Find an eligible binding (``accepts_proactive``). Without one there
   is nowhere to push, so we log and return.
4. Hand a ``ProactiveContext`` to the decider (LLM or stub). If it
   says no, log and return.
5. Append the generated message to the binding's conversation as an
   ``assistant`` turn and push to the platform. Failures are logged
   as ``ERRORED`` attempts so the operator can see them in the UI.

Every exit path writes a ``ProactiveAttempt`` — operators need the log
to debug "why didn't the character message me?" or "why did it again".
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import datetime, timezone, tzinfo
from typing import TYPE_CHECKING

from kokoro_link.contracts.calendar_context import CalendarContextPort
from kokoro_link.contracts.clock import ClockPort, ensure_utc
from kokoro_link.contracts.weather_context import WeatherContextPort
from kokoro_link.contracts.dialogue_summarizer import DialogueSummarizerPort
from kokoro_link.contracts.emotion import EmotionEventRepositoryPort
from kokoro_link.contracts.goal_repository import GoalRepositoryPort
from kokoro_link.contracts.initial_relationship import (
    CharacterOperatorRelationshipSeedRepositoryPort,
)
from kokoro_link.contracts.memory import MemoryRepositoryPort
from kokoro_link.contracts.novelty_gate import (
    NoveltyGateContext,
    NoveltyGatePort,
    NoveltyVerdict,
)
from kokoro_link.contracts.observability import (
    TurnRecorderPort,
    TurnRecordingDraft,
)
from kokoro_link.contracts.messaging import (
    ChannelAdapterPort,
    ChannelBindingRepositoryPort,
    MessagingAccountRepositoryPort,
    OutboundAttachment,
    OutboundMessage,
)
from kokoro_link.contracts.prompt import PromptToolDescriptor
from kokoro_link.contracts.register_profile import (
    RegisterProfileContext,
    RegisterProfilePort,
)
from kokoro_link.contracts.reply_quality import ReplyDiversityEvidence
from kokoro_link.contracts.tool import ToolRegistryPort
from kokoro_link.contracts.proactive import (
    ProactiveAttemptRepositoryPort,
    ProactiveContext,
    ProactiveDecision,
    ProactiveDeciderPort,
    ProactiveGatePort,
)
from kokoro_link.contracts.proactive_intention import (
    ProactiveIntentionDecision,
    ProactiveIntentionJudgePort,
)
from kokoro_link.contracts.repositories import (
    CharacterRepositoryPort,
    ConversationRepositoryPort,
)
from kokoro_link.application.services.proactive_event_bus import (
    ProactiveEvent,
    ProactiveEventBus,
)
from kokoro_link.application.services.subscription_access_guard import (
    SubscriptionAccessGuard,
)
from kokoro_link.application.services.location_context import (
    calendar_region_from_operator,
    prompt_location_fact,
    weather_location_from_operator,
)
from kokoro_link.application.services.outbound_message_segments import (
    send_segmented_outbound,
)
from kokoro_link.application.services.persona_curiosity_observability import (
    persona_curiosity_plan_summary,
)
from kokoro_link.domain.entities.character import Character
from kokoro_link.domain.entities.character_operator_relationship_seed import (
    CharacterOperatorRelationshipSeed,
)
from kokoro_link.domain.entities.channel_binding import ChannelBinding
from kokoro_link.domain.entities.conversation import (
    Message,
    MessageAttachment,
    MessageRole,
    SOURCE_WEB,
)
from kokoro_link.domain.entities.emotion_event import (
    CAUSE_PROACTIVE_ATTEMPT,
    EmotionEvent,
)
from kokoro_link.domain.entities.messaging_account import MessagingAccount
from kokoro_link.domain.entities.operator_profile import (
    DEFAULT_OPERATOR_ID,
    OperatorProfile,
)
from kokoro_link.domain.entities.memory_item import MemoryItem
from kokoro_link.domain.entities.proactive_attempt import ProactiveAttempt
from kokoro_link.domain.entities.story_event import StoryEvent
from kokoro_link.domain.value_objects.goal_status import GoalStatus
from kokoro_link.domain.value_objects.platform import Platform
from kokoro_link.domain.value_objects.content_flow import (
    CONTENT_TOLERANCE_FRONTIER,
    sanitize_messages_for_tolerance,
)
from kokoro_link.domain.services.address_resolver import resolve_character_address
from kokoro_link.domain.value_objects.proactive_outcome import ProactiveOutcome
from kokoro_link.domain.value_objects.proactive_trigger import ProactiveTrigger
from kokoro_link.domain.value_objects.resolved_address import AddressProvenance
from kokoro_link.domain.value_objects.timezone import timezone_for_id
from kokoro_link.infrastructure.prompt.initial_relationship import (
    render_initial_relationship_seed_lines,
)
from kokoro_link.infrastructure.prompt.timing_utils import (
    format_relative_past_label,
)

if TYPE_CHECKING:
    from kokoro_link.application.services.deferred_intent_service import (
        DeferredIntentService,
    )
    from kokoro_link.contracts.operator_address_preference import (
        OperatorAddressPreferenceRepositoryPort,
    )
    from kokoro_link.application.services.event_seed_dispenser import (
        EventSeedDispenser,
    )
    from kokoro_link.application.services.rest_recovery_refresher import (
        RestRecoveryRefresher,
    )
    from kokoro_link.application.services.schedule_service import ScheduleService
    from kokoro_link.application.services.state_tracker import StateChangeTracker
    from kokoro_link.application.services.tool_orchestrator import ToolOrchestrator
    from kokoro_link.application.services.story_arc_service import StoryArcService
    from kokoro_link.application.services.story_event_service import StoryEventService
    from kokoro_link.application.services.persona_curiosity_service import (
        PersonaCuriosityService,
    )
    from kokoro_link.application.services.notification_service import (
        NotificationService,
    )
    from kokoro_link.contracts.persona_curiosity import (
        PersonaCuriosityPlan,
        PersonaCuriosityPlannerPort,
    )

_LOGGER = logging.getLogger(__name__)

# Direction-B address resolutions worth surfacing in the proactive prompt:
# an explicit per-character seed name or an observed salutation. A bare
# character-name (or empty) resolution is dropped so the cold-start prompt
# stays quiet about an unobserved salutation.
_SEED_OR_OBSERVED_PROVENANCE = frozenset(
    {AddressProvenance.EXPLICIT_SEED, AddressProvenance.OBSERVED_PREFERENCE},
)

# How many of the character's own recent SENT pushes to surface. Tuned
# to span several days at the default daily-limit of 3 (≈ 2.5 days) so a
# "ignored for days" streak stays visible to the decider; the prompt
# itself only quotes the first few verbatim to bound length.
_RECENT_SENT_LIMIT = 8


class ProactiveDispatcher:
    def __init__(
        self,
        *,
        character_repository: CharacterRepositoryPort,
        conversation_repository: ConversationRepositoryPort,
        account_repository: MessagingAccountRepositoryPort,
        binding_repository: ChannelBindingRepositoryPort,
        attempt_repository: ProactiveAttemptRepositoryPort,
        gate: ProactiveGatePort,
        decider: ProactiveDeciderPort,
        adapters: dict[Platform, ChannelAdapterPort],
        intention_judge: ProactiveIntentionJudgePort | None = None,
        schedule_resolver: "ScheduleResolver | None" = None,
        memory_repository: MemoryRepositoryPort | None = None,
        goal_repository: GoalRepositoryPort | None = None,
        story_event_service: "StoryEventService | None" = None,
        story_arc_service: "StoryArcService | None" = None,
        state_tracker: "StateChangeTracker | None" = None,
        rest_recovery_refresher: "RestRecoveryRefresher | None" = None,
        tool_registry: ToolRegistryPort | None = None,
        tool_orchestrator: "ToolOrchestrator | None" = None,
        event_bus: ProactiveEventBus | None = None,
        dialogue_summarizer: DialogueSummarizerPort | None = None,
        event_seed_dispenser: "EventSeedDispenser | None" = None,
        calendar_context_port: CalendarContextPort | None = None,
        weather_context_port: "WeatherContextPort | None" = None,
        schedule_service: "ScheduleService | None" = None,
        operator_persona_service=None,  # noqa: ANN001 - optional app service
        relationship_seed_repository: (
            CharacterOperatorRelationshipSeedRepositoryPort | None
        ) = None,
        persona_curiosity_service: "PersonaCuriosityService | None" = None,
        persona_curiosity_planner: "PersonaCuriosityPlannerPort | None" = None,
        operator_profile_service=None,  # noqa: ANN001 - optional, resolves primary_language
        public_base_url: str = "",
        public_base_url_provider: Callable[[], Awaitable[str]] | None = None,
        local_tz: tzinfo | None = None,
        turn_recorder: TurnRecorderPort | None = None,
        emotion_event_repository: EmotionEventRepositoryPort | None = None,
        deferred_intent_service: "DeferredIntentService | None" = None,
        address_preference_repository: "OperatorAddressPreferenceRepositoryPort | None" = None,
        clock: ClockPort | None = None,
        prompt_pack_hash_provider: Callable[[], str] | None = None,
        notification_service: "NotificationService | None" = None,
        register_profiler: RegisterProfilePort | None = None,
        register_profile_enabled: bool = False,
        reply_quality_gate: NoveltyGatePort | None = None,
        reply_quality_gate_enabled: bool = False,
        reply_quality_gate_max_retries: int = 1,
        subscription_access_guard: SubscriptionAccessGuard | None = None,
    ) -> None:
        self._characters = character_repository
        self._conversations = conversation_repository
        self._accounts = account_repository
        self._bindings = binding_repository
        self._attempts = attempt_repository
        self._gate = gate
        self._decider = decider
        self._intention_judge = intention_judge
        self._adapters = {p.value: a for p, a in adapters.items()}
        self._schedule_resolver = schedule_resolver
        self._memories = memory_repository
        self._goals = goal_repository
        self._story_event_service = story_event_service
        self._story_arc_service = story_arc_service
        self._state_tracker = state_tracker
        self._rest_recovery_refresher = rest_recovery_refresher
        self._tool_registry = tool_registry
        self._tool_orchestrator = tool_orchestrator
        self._event_bus = event_bus
        self._dialogue_summarizer = dialogue_summarizer
        self._event_seed_dispenser = event_seed_dispenser
        self._calendar_context_port = calendar_context_port
        self._weather_context_port = weather_context_port
        self._schedule_service = schedule_service
        self._operator_persona_service = operator_persona_service
        self._relationship_seed_repository = relationship_seed_repository
        self._persona_curiosity_service = persona_curiosity_service
        self._persona_curiosity_planner = persona_curiosity_planner
        # FRONTEND_I18N_PLAN §使用者主要語言 — surface the character
        # owner's pinned content language to the decider so proactive
        # openers match chat language. Optional so legacy single-user
        # deploys without auth still wire cleanly; falls back to
        # "zh-TW" when missing.
        self._operator_profile_service = operator_profile_service
        self._public_base_url = public_base_url.rstrip("/")
        self._public_base_url_provider = public_base_url_provider
        # ``count_sent_today`` uses ``now.replace(hour=0, ...)`` to find
        # the start of "today". That means the tzinfo of whatever we
        # pass in is the day boundary — passing UTC would mean daily
        # limits reset at 08:00 local for GMT+8 operators, not midnight.
        # Pinning to the same local_tz that ScheduleService uses makes
        # "today" match what the operator expects.
        self._local_tz = local_tz or timezone.utc
        self._turn_recorder = turn_recorder
        self._prompt_pack_hash_provider = prompt_pack_hash_provider
        self._emotion_events = emotion_event_repository
        self._deferred_intents = deferred_intent_service
        self._address_preferences = address_preference_repository
        self._clock = clock
        self._notification_service = notification_service
        self._register_profiler = register_profiler
        self._register_profile_enabled = bool(register_profile_enabled)
        self._reply_quality_gate = reply_quality_gate
        self._reply_quality_gate_enabled = bool(reply_quality_gate_enabled)
        self._reply_quality_gate_max_retries = max(
            0,
            int(reply_quality_gate_max_retries),
        )
        self._subscription_access_guard = subscription_access_guard

    async def evaluate(
        self,
        *,
        character_id: str,
        trigger: ProactiveTrigger,
        now: datetime | None = None,
    ) -> ProactiveAttempt:
        when = self._resolve_now(now)
        character = await self._characters.get(character_id)
        if character is None:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.ERRORED,
                reason="character not found",
                now=when,
            )
        if (
            self._subscription_access_guard is not None
            and not await self._subscription_access_guard.is_character_allowed(
                character,
            )
        ):
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.DISABLED,
                reason="subscription is inactive",
                now=when,
            )

        if not character.proactive_enabled:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.DISABLED,
                reason="proactive_enabled is False",
                now=when,
            )

        relationship_seed = await self._load_relationship_seed(character)
        if (
            _requires_user_started_interaction(trigger)
            and not await self._has_user_started_interaction(character)
            and not _seed_allows_pre_message_proactive(relationship_seed)
        ):
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.GATE_BLOCKED,
                reason="waiting for first user message",
                now=when,
            )

        # Rest recovery is lazy in the chat path — without this the
        # scheduler would keep seeing stale fatigue/energy and the gate
        # would block proactive sends indefinitely for any character
        # who went to bed exhausted and hasn't chatted since. We apply
        # the same exponential decay here, persist when it changes, and
        # record a REST_RECOVERY snapshot so the state-history UI shows
        # why energy crept up without a user turn.
        character = await self._apply_rest_recovery(character, when)

        # Cooldown is anchored on "last time we actually spent LLM budget",
        # not any attempt — otherwise every gate-blocked tick would
        # reset the cooldown and it would never lapse in practice.
        last_passing = (
            await self._attempts.latest_passing_gate_for_character(character_id)
        )
        last_attempt = await self._attempts.latest_for_character(character_id)
        operator = await self._load_operator_profile(character)
        operator_tz = _timezone_for_operator(operator, self._local_tz)
        local_now = when.astimezone(operator_tz)
        initial_relationship_lines = render_initial_relationship_seed_lines(
            relationship_seed,
        )
        sent_today = await self._attempts.count_sent_today(
            character_id, now=local_now,
        )
        idle_minutes = _compute_idle_minutes(character, when)
        current_activity, upcoming, schedule, just_finished_activity = (
            await self._resolve_schedule(character, when)
        )

        verdict = await self._gate.check(
            character=character,
            trigger=trigger,
            now=when,
            sent_today=sent_today,
            last_attempt_at=(
                last_passing.decided_at if last_passing else None
            ),
            idle_minutes=idle_minutes,
            current_activity=current_activity,
            local_tz=operator_tz,
        )
        if not verdict.passed:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.GATE_BLOCKED,
                reason=verdict.reason,
                now=when,
            )

        eligible = await self._find_eligible_binding(character_id)
        web_enabled = bool(character.accepts_web_proactive)
        if eligible is None and not web_enabled:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.NO_BINDING,
                reason=(
                    "no binding has accepts_proactive=True and "
                    "accepts_web_proactive is False"
                ),
                now=when,
            )
        binding, account = eligible if eligible else (None, None)

        recent_memories_text = await self._load_recent_memories_text(
            character_id, when,
        )
        active_goals_text = await self._load_active_goals_text(character_id)
        available_tools = self._describe_tools(character)
        story_events = await self._load_story_events(character, when)
        recent_sent_attempts = await self._load_recent_sent_attempts(character_id)
        recent_dialogue_summary = await self._summarize_recent_dialogue(character)
        persona_curiosity_plan = await self._load_persona_curiosity_plan(
            character=character,
            operator=operator,
            recent_dialogue_summary=recent_dialogue_summary,
            initial_relationship_lines=initial_relationship_lines,
            now=when,
        )
        persona_curiosity_metadata = persona_curiosity_plan_summary(
            persona_curiosity_plan,
            surface="proactive",
        )
        active_arc, upcoming_beats = await self._ensure_active_arc(
            character, when, operator_tz,
        )
        seed_title, seed_summary, seed_source, seed_locale, seed_item_id = (
            await self._claim_event_seed(character)
        )

        calendar_context = self._describe_calendar(
            when, operator_tz, operator=operator,
        )
        weather_context = await self._describe_weather(when, operator=operator)
        upcoming_day_schedules = await self._load_upcoming_day_schedules(
            character.id, when, operator_tz,
        )
        operator_persona_lines = await self._load_operator_persona_lines(
            character,
        )
        # HUMANIZATION_ROADMAP §3.4 — surface still-active deferred motives
        # so the intention judge can re-evaluate timing on a re-tick.
        deferred_intents = await self._load_active_deferred_intents(
            character_id=character.id, when=when,
        )
        # HUMANIZATION_ROADMAP §4.2 — observed register / address preference.
        address_preference = await self._load_address_preference(
            character_id=character.id,
        )
        # Resolve how the player addresses this character (seed > observed)
        # so an explicit per-character seed name leads the proactive prompt.
        # A bare character-name fallback is suppressed so the cold-start
        # prompt stays quiet about an unobserved salutation.
        resolved_character = resolve_character_address(
            seed=relationship_seed,
            preference=address_preference,
            character=character,
        )
        resolved_character_salutation = (
            resolved_character.primary
            if resolved_character.provenance in _SEED_OR_OBSERVED_PROVENANCE
            else None
        )
        operator_primary_language = _language_for_operator(operator)
        operator_location_context = prompt_location_fact(operator)
        unanswered_streak = _count_unanswered_streak(
            recent_sent_attempts, idle_minutes=idle_minutes, now=when,
        )
        context = ProactiveContext(
            character=character,
            trigger=trigger,
            now=when,
            current_activity=current_activity,
            upcoming_activities=list(upcoming),
            schedule=schedule,
            just_finished_activity=just_finished_activity,
            idle_minutes=idle_minutes,
            sent_today=sent_today,
            last_proactive_at=(
                last_passing.decided_at if last_passing
                else (last_attempt.decided_at if last_attempt else None)
            ),
            recent_memories_text=recent_memories_text,
            active_goals_text=active_goals_text,
            available_tools=available_tools,
            story_events=story_events,
            recent_dialogue_summary=recent_dialogue_summary,
            active_arc=active_arc,
            upcoming_beats=upcoming_beats,
            recent_sent_attempts=recent_sent_attempts,
            unanswered_streak=unanswered_streak,
            world_event_seed_title=seed_title,
            world_event_seed_summary=seed_summary,
            world_event_seed_source=seed_source,
            world_event_seed_locale=seed_locale,
            operator_location_context=operator_location_context,
            calendar_context=calendar_context,
            weather_context=weather_context,
            upcoming_day_schedules=tuple(upcoming_day_schedules),
            operator_persona_lines=tuple(operator_persona_lines),
            initial_relationship_lines=tuple(initial_relationship_lines),
            persona_curiosity_plan=persona_curiosity_plan,
            deferred_intents=deferred_intents,
            address_preference=address_preference,
            resolved_character_salutation=resolved_character_salutation,
            operator_primary_language=operator_primary_language,
            local_tz=operator_tz,
        )
        if self._intention_judge is not None:
            try:
                intention = await self._intention_judge.judge(context)
            except Exception:
                _LOGGER.exception("proactive intention judge crashed")
                await self._release_event_seed(character_id, seed_item_id)
                return await self._log(
                    character_id=character_id,
                    trigger=trigger,
                    outcome=ProactiveOutcome.ERRORED,
                    reason="intention judge raised",
                    metadata={"persona_curiosity": persona_curiosity_metadata},
                    now=when,
                )
            if not intention.should_consume_slot:
                # HUMANIZATION_ROADMAP §3.4 — park the motive so the
                # next tick can re-evaluate timing instead of forgetting
                # an authentic urge after one bad moment.
                await self._record_deferred_intent(
                    character_id=character_id,
                    trigger=trigger,
                    decision=intention,
                    now=when,
                )
                await self._release_event_seed(character_id, seed_item_id)
                return await self._log(
                    character_id=character_id,
                    trigger=trigger,
                    outcome=ProactiveOutcome.INTENTION_SKIPPED,
                    reason=_format_intention_skip_reason(intention),
                    metadata={"persona_curiosity": persona_curiosity_metadata},
                    now=when,
                )
        try:
            decision = await self._decider.decide(context)
        except Exception:
            _LOGGER.exception("proactive decider crashed")
            await self._release_event_seed(character_id, seed_item_id)
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.ERRORED,
                reason="decider raised",
                metadata={"persona_curiosity": persona_curiosity_metadata},
                now=when,
            )

        quality_metadata: dict[str, object] = {}
        if decision.should_send and decision.message:
            decision, quality_metadata = await self._gate_proactive_decision(
                context=context,
                decision=decision,
                character=character,
            )

        if not decision.should_send or not decision.message:
            await self._release_event_seed(character_id, seed_item_id)
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.DECIDER_SKIPPED,
                reason=decision.reason,
                metadata={
                    "persona_curiosity": persona_curiosity_metadata,
                    **quality_metadata,
                },
                now=when,
            )

        # Run any tool calls the decider asked for *before* pushing
        # the outbound. Attachments from each successful call get
        # merged into the outbound payload; failures are silently
        # dropped (the text message still goes out on its own so the
        # user isn't left with an empty push waiting on a broken
        # ComfyUI). Audit rows are written by the orchestrator.
        attachments = await self._execute_decision_tools(
            character=character,
            decision=decision,
            conversation_id=(binding.conversation_id if binding else None),
        )

        # Fan out: web (if opted in) + messaging binding (if any).
        # A failure on one target must not block the other — e.g. a
        # dead Telegram bot token shouldn't swallow the web badge
        # update the user is actually watching for.
        delivered = 0
        web_delivered = False
        external_delivered = False
        binding_id_for_log: str | None = None
        if web_enabled:
            try:
                await self._deliver_web(
                    character=character,
                    text=decision.message,
                    attachments=attachments,
                    when=when,
                )
                delivered += 1
                web_delivered = True
            except Exception:
                _LOGGER.exception("proactive web delivery crashed")
        if binding is not None and account is not None:
            try:
                await self._deliver(
                    binding=binding, account=account, text=decision.message,
                    attachments=attachments,
                    locale=operator_primary_language,
                )
                delivered += 1
                external_delivered = True
                binding_id_for_log = binding.id
            except Exception:
                _LOGGER.exception("proactive messaging delivery crashed")

        if delivered == 0:
            await self._release_event_seed(character_id, seed_item_id)
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.ERRORED,
                reason="delivery raised",
                binding_id=(binding.id if binding else None),
                message=decision.message,
                metadata={
                    "persona_curiosity": persona_curiosity_metadata,
                    **quality_metadata,
                },
                now=when,
            )

        # HUMANIZATION_ROADMAP §3.4 — a successful send folds the
        # character's pending motives into reality; mark them consumed
        # so they stop re-surfacing in subsequent judge calls.
        await self._consume_deferred_intents(context.deferred_intents, now=when)
        await self._notify_web_push(
            character=character,
            message=decision.message,
            web_delivered=web_delivered,
            external_delivered=external_delivered,
        )

        return await self._log(
            character_id=character_id,
            trigger=trigger,
            outcome=ProactiveOutcome.SENT,
            reason=decision.reason,
            binding_id=binding_id_for_log,
            message=decision.message,
            metadata={
                "persona_curiosity": persona_curiosity_metadata,
                **quality_metadata,
            },
            now=when,
        )

    async def _gate_proactive_decision(
        self,
        *,
        context: ProactiveContext,
        decision: ProactiveDecision,
        character: Character,
    ) -> tuple[ProactiveDecision, dict[str, object]]:
        if (
            not self._reply_quality_gate_enabled
            or self._reply_quality_gate is None
        ):
            return decision, {}
        profile = await self._profile_proactive_register(context, character)
        diversity = _proactive_diversity_evidence(context)
        verdict = await self._evaluate_proactive_quality_gate(
            context=context,
            decision=decision,
            character=character,
            register_profile=profile,
            diversity_evidence=diversity,
        )
        retry_count = 0
        selected = decision
        if (
            verdict is not None
            and not verdict.passes
            and self._reply_quality_gate_max_retries > 0
        ):
            retry_count = 1
            retry_context = replace(
                context,
                recent_dialogue_summary=(
                    f"{context.recent_dialogue_summary}\n"
                    f"上一輪主動訊息品質問題：{verdict.feedback}"
                ).strip(),
            )
            try:
                retry_decision = await self._decider.decide(retry_context)
            except Exception:
                _LOGGER.exception("proactive quality retry decider crashed")
            else:
                if retry_decision.should_send and retry_decision.message:
                    selected = retry_decision
        return selected, {
            "reply_quality_gate": _quality_gate_metadata(
                verdict,
                enabled=True,
                retry_count=retry_count,
            ),
            "register_profile": _register_profile_metadata(
                profile,
                enabled=self._register_profile_enabled,
            ),
            "diversity": _diversity_metadata(diversity),
        }

    async def _profile_proactive_register(
        self,
        context: ProactiveContext,
        character: Character,
    ):
        if (
            not self._register_profile_enabled
            or self._register_profiler is None
        ):
            return None
        profile_context = RegisterProfileContext(
            character_id=character.id,
            operator_id=getattr(character, "user_id", DEFAULT_OPERATOR_ID),
            latest_user_message=(
                context.recent_dialogue_summary
                or f"proactive trigger: {context.trigger.value}"
            ),
            recent_dialogue_summary=context.recent_dialogue_summary,
            relationship_context=tuple([
                *context.operator_persona_lines,
                *context.initial_relationship_lines,
            ]),
            content_tolerance=CONTENT_TOLERANCE_FRONTIER,
        )
        try:
            return await self._register_profiler.profile(
                profile_context,
                character=character,
            )
        except Exception:
            _LOGGER.exception("proactive register profiler failed open")
            return None

    async def _evaluate_proactive_quality_gate(
        self,
        *,
        context: ProactiveContext,
        decision: ProactiveDecision,
        character: Character,
        register_profile,
        diversity_evidence: ReplyDiversityEvidence,
    ) -> NoveltyVerdict | None:
        if self._reply_quality_gate is None:
            return None
        gate_context = NoveltyGateContext(
            character_id=character.id,
            operator_id=getattr(character, "user_id", DEFAULT_OPERATOR_ID),
            response_text=decision.message or "",
            known_material=tuple(
                item for item in (
                    context.recent_memories_text,
                    context.active_goals_text,
                    context.recent_dialogue_summary,
                    context.world_event_seed_summary,
                )
                if item and item.strip()
            ),
            recent_self_lines=tuple(
                attempt.message or ""
                for attempt in context.recent_sent_attempts[:4]
                if attempt.message
            ),
            self_repetition_hint="",
            latest_user_message=context.recent_dialogue_summary,
            content_tolerance=CONTENT_TOLERANCE_FRONTIER,
            register_profile=register_profile,
            diversity_evidence=diversity_evidence,
            persona_context=(
                f"性格：{', '.join(character.personality)}",
                f"說話風格：{character.speaking_style}",
                *context.initial_relationship_lines,
            ),
        )
        try:
            return await self._reply_quality_gate.evaluate(
                gate_context,
                character=character,
            )
        except Exception as exc:
            _LOGGER.exception("proactive reply quality gate failed open")
            return NoveltyVerdict.pass_open(repr(exc))

    async def _apply_rest_recovery(
        self, character: Character, now: datetime,
    ) -> Character:
        """Event-path recovery: POST_TURN / ACTIVITY_TRANSITION triggers
        don't go through ``_tick_all``, so we refresh here too.

        Delegates to the shared ``RestRecoveryRefresher`` to keep the
        write/snapshot policy consistent across call sites. When no
        refresher is wired (old tests), falls back to a local compute
        so the gate at least sees up-to-date values in-memory.
        """
        if self._rest_recovery_refresher is not None:
            return await self._rest_recovery_refresher.refresh(
                character, now=now,
            )
        from kokoro_link.infrastructure.state.recovery import apply_rest_recovery

        recovered_state = apply_rest_recovery(character.state, now=now)
        if recovered_state is character.state:
            return character
        return character.with_state(recovered_state)

    async def _find_eligible_binding(
        self, character_id: str,
    ) -> tuple[ChannelBinding, MessagingAccount] | None:
        accounts = await self._accounts.list_for_character(character_id)
        candidates: list[tuple[ChannelBinding, MessagingAccount]] = []
        for account in accounts:
            if not account.enabled:
                continue
            bindings = await self._bindings.list_for_account(account.id)
            for binding in bindings:
                if binding.enabled and binding.accepts_proactive:
                    candidates.append((binding, account))
        if not candidates:
            return None
        # Prefer the most recently touched binding.
        candidates.sort(key=lambda pair: pair[0].updated_at, reverse=True)
        return candidates[0]

    async def _deliver(
        self,
        *,
        binding: ChannelBinding,
        account: MessagingAccount,
        text: str,
        attachments: tuple[OutboundAttachment, ...] = (),
        locale: str = "zh-TW",
    ) -> None:
        conversation_id = binding.conversation_id
        updated_binding = binding
        if conversation_id is None:
            from kokoro_link.domain.entities.conversation import Conversation

            conversation = Conversation.start(
                character_id=account.character_id,
                source=account.platform.value,
            )
            await self._conversations.save(conversation)
            updated_binding = binding.with_conversation(conversation.id)
            await self._bindings.save(updated_binding)
            conversation_id = conversation.id

        conversation = await self._conversations.get(conversation_id)
        if conversation is None:
            from kokoro_link.domain.entities.conversation import Conversation

            conversation = Conversation.start(
                character_id=account.character_id,
                source=account.platform.value,
            )
            await self._conversations.save(conversation)
            updated_binding = updated_binding.with_conversation(conversation.id)
            await self._bindings.save(updated_binding)
        appended = conversation.append(
            Message(role=MessageRole.ASSISTANT, content=text),
        )
        await self._conversations.save(appended)

        adapter = self._adapters.get(account.platform.value)
        if adapter is None:
            raise RuntimeError(
                f"no adapter registered for platform {account.platform.value}",
            )
        await send_segmented_outbound(
            adapter,
            OutboundMessage(
                platform=account.platform,
                chat_ref=updated_binding.chat_ref,
                text=text,
                credentials=account.credentials,
                attachments=attachments,
                locale=locale,
            ),
        )

    async def _deliver_web(
        self,
        *,
        character: Character,
        text: str,
        attachments: tuple[OutboundAttachment, ...],
        when: datetime,
    ) -> None:
        """Write the proactive message to the character's web thread,
        bump the unread badge, and publish an event for SSE clients.

        Reuses whatever ``source="web"`` conversation the user already
        has open — so when they refresh / reconnect the message appears
        inline with their normal chat history, not in a parallel log.
        """
        from kokoro_link.domain.entities.conversation import Conversation

        conversation = await self._conversations.latest_for_character(
            character.id, source=SOURCE_WEB,
        )
        if conversation is None:
            conversation = Conversation.start(
                character_id=character.id, source=SOURCE_WEB,
            )

        # Demote absolute URLs that point at our own ``public_base_url``
        # back to server-relative form before persisting into the web
        # conversation. The collection step in ``_collect_tool_attachments``
        # absolute-ifies for TG/LINE (their servers fetch by URL), but
        # the web frontend should fetch from whatever origin the
        # operator opened the browser on — otherwise an internal-LAN
        # visit gets pinned to the external DDNS host the bot uses,
        # round-trips through hairpin NAT, and times out. Absolute URLs
        # pointing at OTHER hosts (e.g. external CDN) pass through
        # untouched so they still load correctly.
        public_base_url = await self._resolve_public_base_url()
        message_attachments = tuple(
            MessageAttachment(
                kind=att.kind,
                url=self._demote_to_relative(att.url, public_base_url),
                mime_type=att.mime_type,
                caption=att.caption,
            )
            for att in attachments
        )
        appended = conversation.append(
            Message(
                role=MessageRole.ASSISTANT,
                content=text,
                attachments=message_attachments,
            ),
        )
        await self._conversations.save(appended)

        next_count = character.unread_proactive_count + 1
        updated_character = character.with_unread_proactive(next_count)
        await self._characters.save(updated_character)

        if self._event_bus is not None:
            await self._event_bus.publish(
                ProactiveEvent(
                    character_id=character.id,
                    conversation_id=appended.id,
                    message=text,
                    created_at=when,
                    unread_count=updated_character.unread_proactive_count,
                ),
            )

    def _demote_to_relative(self, url: str, public_base_url: str | None = None) -> str:
        """Strip our own ``public_base_url`` prefix to keep the URL
        portable across access origins.

        Used when persisting tool attachments into the web conversation
        — the same URL might later be served via internal LAN domain,
        external DDNS, tunneled localhost in dev, etc. Keeping it
        relative means whichever origin the operator visits, the
        ``<img>`` resolves against that origin and dodges hairpin NAT.

        URLs not starting with our base (external CDN, third-party
        hosts) and already-relative URLs pass through untouched."""
        base_url = (public_base_url or self._public_base_url).rstrip("/")
        if not base_url:
            return url
        if url.startswith(base_url):
            tail = url[len(base_url):]
            if tail.startswith("/"):
                return tail
        return url

    def _describe_tools(
        self, character: Character,
    ) -> tuple[PromptToolDescriptor, ...]:
        if self._tool_registry is None:
            return ()
        tools = self._tool_registry.list_for_character(character)
        return tuple(
            PromptToolDescriptor(
                name=t.name,
                description=t.description,
                parameters_schema=t.parameters_schema,
            )
            for t in tools
        )

    async def _execute_decision_tools(
        self,
        *,
        character: Character,
        decision,  # ProactiveDecision (avoid re-import)
        conversation_id: str | None,
    ) -> tuple[OutboundAttachment, ...]:
        if not decision.tool_calls or self._tool_orchestrator is None:
            return ()
        collected: list[OutboundAttachment] = []
        public_base_url = await self._resolve_public_base_url()
        for call in decision.tool_calls:
            try:
                _, result = await self._tool_orchestrator.execute(
                    character=character,
                    call=call,
                    conversation_id=conversation_id,
                )
            except Exception:
                _LOGGER.exception(
                    "proactive tool %s crashed", call.name,
                )
                continue
            if not result.ok:
                _LOGGER.info(
                    "proactive tool %s failed: %s", call.name, result.error,
                )
                continue
            for att in result.attachments:
                url = att.url
                if url.startswith("/"):
                    if not public_base_url:
                        _LOGGER.warning(
                            "dropping proactive attachment %s for %s — "
                            "messaging public base URL is not set, external "
                            "platforms cannot fetch a server-relative URL. "
                            "Set Admin Channel settings Public Base URL or "
                            "APP_BASE_URL",
                            url, att.kind,
                        )
                        continue
                    url = f"{public_base_url}{url}"
                collected.append(
                    OutboundAttachment(
                        kind=att.kind,
                        url=url,
                        mime_type=att.mime_type,
                        caption=att.caption,
                    ),
                )
        return tuple(collected)

    async def _resolve_public_base_url(self) -> str:
        if self._public_base_url_provider is None:
            return self._public_base_url
        try:
            resolved = await self._public_base_url_provider()
        except Exception:
            _LOGGER.exception(
                "proactive public base URL provider failed; using env fallback",
            )
            return self._public_base_url
        if not isinstance(resolved, str):
            return self._public_base_url
        resolved = resolved.strip().rstrip("/")
        return resolved or self._public_base_url

    async def _resolve_schedule(
        self, character: Character, now: datetime,
    ):
        if self._schedule_resolver is None:
            return None, [], None, None
        try:
            return await self._schedule_resolver(character, now)
        except Exception:
            _LOGGER.exception("proactive schedule resolver crashed")
            return None, [], None, None

    async def _load_recent_memories_text(
        self, character_id: str, now: datetime | None = None,
    ) -> str:
        if self._memories is None:
            return ""
        try:
            items = await self._memories.query(character_id, limit=6)
        except Exception:
            _LOGGER.exception("proactive: memory repository query failed")
            return ""
        return _format_memories(items, now=now)

    async def _load_upcoming_day_schedules(
        self, character_id: str, when: datetime, local_tz: tzinfo,
    ) -> list:
        """Read pre-planned tomorrow + day-after schedules.

        Read-only — the proactive scheduler tick is the eager
        generator (``ensure_window``); this dispatcher path only
        renders what the repository already has. Returns an empty
        list when the schedule service is unwired or no upcoming
        days are present.
        """
        if self._schedule_service is None:
            return []
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        target_date = when.astimezone(local_tz).date()
        try:
            return await self._schedule_service.load_upcoming_schedules(
                character_id, start_after=target_date,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: load_upcoming_schedules failed character=%s",
                character_id,
            )
            return []

    async def _load_operator_profile(self, character) -> OperatorProfile | None:  # noqa: ANN001
        service = self._operator_profile_service
        if service is None:
            return None
        user_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            return await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return None

    async def _load_operator_language(self, character) -> str:  # noqa: ANN001
        """Resolve the BCP 47 ``primary_language`` of the character's
        owner. ``"zh-TW"`` is the deterministic fallback for any path
        that can't reach the operator profile — matches the alembic
        backfill so behaviour is consistent."""
        default = "zh-TW"
        service = self._operator_profile_service
        if service is None:
            return default
        user_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            operator = await service.get_for_user(user_id)
        except Exception:  # pragma: no cover - defensive
            return default
        if operator is None:
            return default
        lang = getattr(operator, "primary_language", "") or ""
        return lang.strip() or default

    async def _load_operator_timezone(self, character) -> tzinfo:  # noqa: ANN001
        default = self._local_tz
        service = self._operator_profile_service
        if service is None:
            return default
        user_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            operator = await service.get_for_user(user_id)
            return timezone_for_id(getattr(operator, "timezone_id", None))
        except Exception:  # pragma: no cover - defensive
            return default

    async def _load_operator_persona_lines(self, character: Character) -> list[str]:
        service = self._operator_persona_service
        if service is None:
            return []
        operator_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            persona = await service.get_current(character.id, operator_id)
            return list(service.render_for_prompt(persona))
        except Exception:
            _LOGGER.exception(
                "proactive: operator persona render failed character=%s",
                character.id,
            )
            return []

    async def _load_relationship_seed(
        self, character: Character,
    ) -> CharacterOperatorRelationshipSeed | None:
        if self._relationship_seed_repository is None:
            return None
        operator_id = getattr(character, "user_id", None) or DEFAULT_OPERATOR_ID
        try:
            return await self._relationship_seed_repository.get(
                character_id=character.id,
                operator_id=operator_id,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: relationship seed lookup failed character=%s",
                character.id,
            )
            return None

    async def _load_persona_curiosity_plan(
        self,
        *,
        character: Character,
        operator: OperatorProfile | None,
        recent_dialogue_summary: str,
        now: datetime,
        initial_relationship_lines: list[str] | tuple[str, ...] = (),
    ) -> "PersonaCuriosityPlan | None":
        if (
            self._operator_persona_service is None
            or self._persona_curiosity_service is None
            or self._persona_curiosity_planner is None
        ):
            return None
        operator_id = getattr(operator, "id", None) or DEFAULT_OPERATOR_ID
        try:
            persona = await self._operator_persona_service.get_current(
                character.id,
                operator_id,
            )
            context = await self._persona_curiosity_service.build_context(
                persona=persona,
                surface="proactive",
                recent_dialogue_summary=recent_dialogue_summary,
                initial_relationship_lines=tuple(initial_relationship_lines),
                now=now,
                operator_primary_language=_language_for_operator(operator),
            )
            plan = await self._persona_curiosity_planner.plan(
                context,
                character=character,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: persona curiosity plan failed character=%s",
                character.id,
            )
            return None
        await self._record_persona_curiosity_planned(
            context=context,
            plan=plan,
            now=now,
        )
        return plan

    async def _record_persona_curiosity_planned(
        self,
        *,
        context,
        plan: "PersonaCuriosityPlan",
        now: datetime,
    ) -> None:
        if self._persona_curiosity_service is None:
            return
        try:
            await self._persona_curiosity_service.record_planned_attempt(
                context=context,
                plan=plan,
                now=now,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: persona curiosity planned-attempt record failed",
            )

    def _describe_calendar(
        self,
        when: datetime,
        local_tz: tzinfo,
        *,
        operator: OperatorProfile | None = None,
    ) -> str:
        """Render the real-world calendar block for the local-tz civil
        date that ``when`` falls on.

        Empty string when no calendar port is wired or describe raises
        — the decider section then renders nothing. Logged so operators
        can tell a "missing" block from a "disabled" one.
        """
        if self._calendar_context_port is None:
            return ""
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        target_date = when.astimezone(local_tz).date()
        try:
            return self._calendar_context_port.describe(
                target_date,
                region=calendar_region_from_operator(operator),
            )
        except Exception:
            _LOGGER.exception(
                "proactive: calendar describe failed target=%s", target_date,
            )
            return ""

    async def _describe_weather(
        self,
        when: datetime,
        *,
        operator: OperatorProfile | None = None,
    ) -> str:
        """Async weather counterpart. ``when`` is currently unused by
        the only implemented adapter (Open-Meteo always reports the
        present moment) but threaded through for parity / future use."""
        if self._weather_context_port is None:
            return ""
        try:
            return await self._weather_context_port.describe(
                now=when,
                location=weather_location_from_operator(operator),
            )
        except Exception:
            _LOGGER.exception(
                "proactive: weather describe failed; falling back to empty",
            )
            return ""

    async def _summarize_recent_dialogue(self, character: Character) -> str:
        """Condense the character's latest dialogue for the decider.

        Pulls messages merged across every source (web / telegram /
        line / …) — the character is one person on every channel, so
        the decider sees the same unified timeline as the chat prompt.
        Returns empty string when the summariser is unwired, no
        messages exist, or summarisation fails — the decider treats
        empty as "no dialogue context" and skips the section."""
        if self._dialogue_summarizer is None:
            return ""
        try:
            messages = await self._conversations.recent_messages_for_character(
                character.id, limit=40, exclude_tool_only=True,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: dialogue load failed character=%s", character.id,
            )
            return ""
        if not messages:
            return ""
        messages = sanitize_messages_for_tolerance(
            messages,
            content_tolerance=CONTENT_TOLERANCE_FRONTIER,
        )
        if not messages:
            return ""
        try:
            return await self._dialogue_summarizer.summarize(
                character=character, messages=messages,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: dialogue summarise failed character=%s", character.id,
            )
            return ""

    async def _has_user_started_interaction(self, character: Character) -> bool:
        if character.state.last_active_at is not None:
            return True
        try:
            return await self._conversations.has_user_message_for_character(
                character.id,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: first-user-message lookup failed character=%s",
                character.id,
            )
            return False

    async def _claim_event_seed(
        self, character: Character,
    ) -> tuple[str, str, str, str, str | None]:
        """Try to claim a curated world event for the proactive surface.

        Returns ``("", "", "", "", None)`` when no dispenser is wired, the
        character opted out of world awareness, or no fresh seed is
        available. Tuple shape is ``(title, summary, source, locale,
        item_id)``; ``item_id`` is the inbox item id of the claimed row
        when the claim succeeded — the caller passes it to
        :meth:`_release_event_seed` if the decider ends up not sending,
        so the seed flows back to the next surface instead of being
        burned silently.
        """
        if self._event_seed_dispenser is None:
            return "", "", "", "", None
        if not character.world_awareness_enabled:
            return "", "", "", "", None
        try:
            claimed = await self._event_seed_dispenser.claim(
                character_id=character.id, surface="proactive_message",
            )
        except Exception:
            _LOGGER.exception(
                "proactive: event seed claim failed character=%s", character.id,
            )
            return "", "", "", "", None
        if claimed is None:
            return "", "", "", "", None
        return (
            claimed.event.title or "",
            claimed.event.summary or "",
            claimed.event.source or "",
            claimed.event.locale or "",
            claimed.item.id,
        )

    async def _release_event_seed(
        self, character_id: str, item_id: str | None,
    ) -> None:
        """Counter-part to :meth:`_claim_event_seed`. Best-effort.

        Called when a claim was made but the message never went out
        (decider skipped, decider crashed, no eligible delivery
        binding). Without this the seed is locked to ``proactive_message``
        forever even though no message referenced it, starving feed and
        drama of fresh inbox rows.
        """
        if item_id is None or self._event_seed_dispenser is None:
            return
        try:
            await self._event_seed_dispenser.release(
                item_id=item_id, surface="proactive_message",
            )
        except Exception:
            _LOGGER.exception(
                "proactive: event seed release failed character=%s item=%s",
                character_id, item_id,
            )

    async def _ensure_active_arc(
        self, character: Character, when: datetime, local_tz: tzinfo,
    ):
        """Mirror ChatService: lazy-create the character's active arc so
        the decider sees the same narrative anchor user-chat does.
        Without this the proactive path only had gacha events + dialogue
        summary, and newly created characters had no arc at all — leading
        to openers that ignored whatever arc the user was mid-way through.

        Returns ``(arc, upcoming_beats)``; empty on any failure — arcs
        are colour, never worth aborting a proactive push over."""
        if self._story_arc_service is None:
            return None, ()
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        today = when.astimezone(local_tz).date()
        try:
            arc = await self._story_arc_service.ensure_active_arc(
                character, today=today, auto_start=True,
            )
        except Exception:
            _LOGGER.exception(
                "proactive: arc ensure_active_arc crashed character=%s",
                character.id,
            )
            return None, ()
        if arc is None:
            return None, ()
        try:
            forward = arc.forward_beats(
                after=today, limit=2, include_today=True,
            )
        except Exception:
            _LOGGER.exception("proactive: arc.forward_beats crashed")
            forward = []
        return arc, tuple(forward)

    async def _load_story_events(
        self, character: Character, when: datetime,
    ) -> tuple[StoryEvent, ...]:
        """Today's story events (idempotent ensure). Empty on failure."""
        if self._story_event_service is None:
            return ()
        try:
            report = await self._story_event_service.ensure_today(
                character, now=when,
            )
            return tuple(report.events)
        except Exception:
            _LOGGER.exception("proactive: story ensure_today crashed")
            return ()

    async def _load_active_deferred_intents(
        self,
        *,
        character_id: str,
        when: datetime,
    ) -> tuple:
        """HUMANIZATION_ROADMAP §3.4 helper.

        Returns the still-active deferred motives the intention judge
        previously parked, as a tuple suitable for ``ProactiveContext``.
        Empty when the feature is off, the service is not wired, or no
        motives are pending. All failures collapse to an empty tuple so
        the dispatcher never blocks proactive evaluation on this
        secondary signal.
        """
        if self._deferred_intents is None or not self._deferred_intents.enabled:
            return ()
        try:
            intents = await self._deferred_intents.list_active(
                character_id, DEFAULT_OPERATOR_ID, now=when,
            )
        except Exception:
            _LOGGER.exception(
                "deferred_intent list_active crashed character=%s",
                character_id,
            )
            return ()
        return tuple(intents)

    async def _load_address_preference(
        self,
        *,
        character_id: str,
    ):
        """HUMANIZATION_ROADMAP §4.2 helper.

        Returns the persisted ``OperatorAddressPreference`` for the
        ``(character, default_operator)`` pair, or ``None`` when the
        repository is missing / no row recorded / lookup failed. All
        failures collapse to ``None`` — the prompt builder treats this
        as "no observation, fall back to §3.6 pace".
        """
        if self._address_preferences is None:
            return None
        try:
            return await self._address_preferences.get(
                character_id=character_id, operator_id=DEFAULT_OPERATOR_ID,
            )
        except Exception:
            _LOGGER.exception(
                "address_preference lookup crashed character=%s",
                character_id,
            )
            return None

    async def _record_deferred_intent(
        self,
        *,
        character_id: str,
        trigger: ProactiveTrigger,
        decision: ProactiveIntentionDecision,
        now: datetime,
    ) -> None:
        if self._deferred_intents is None:
            return
        try:
            await self._deferred_intents.record_if_useful(
                character_id=character_id,
                operator_id=DEFAULT_OPERATOR_ID,
                trigger=trigger.value,
                decision=decision,
                now=now,
            )
        except Exception:
            _LOGGER.exception(
                "deferred_intent record crashed character=%s",
                character_id,
            )

    async def _consume_deferred_intents(
        self,
        intents: "tuple",
        *,
        now: datetime,
    ) -> None:
        if self._deferred_intents is None or not intents:
            return
        try:
            await self._deferred_intents.mark_consumed_many(
                [intent.id for intent in intents], now=now,
            )
        except Exception:
            _LOGGER.exception("deferred_intent mark_consumed_many crashed")

    async def _load_recent_sent_attempts(
        self, character_id: str,
    ) -> tuple[ProactiveAttempt, ...]:
        """Grab the most-recent actually-sent proactive attempts.

        The decider needs this to avoid re-generating the same message
        across cooldown windows — the LLM sees the same context every
        tick (personality, state, today's story event) and without the
        tail of its own recent output it happily paraphrases the same
        opener all night. Source-filtered by ``list_recent_sent`` so the
        flood of GATE_BLOCKED audit rows (one per ~5-min tick) can't bury
        cross-day SENT history the way the old over-fetch-and-filter did.
        """
        try:
            sent = await self._attempts.list_recent_sent(
                character_id, limit=_RECENT_SENT_LIMIT,
            )
        except Exception:
            _LOGGER.exception("proactive: recent-sent query failed")
            return ()
        return tuple(sent)

    async def _load_active_goals_text(self, character_id: str) -> str:
        if self._goals is None:
            return ""
        try:
            goals = await self._goals.list_for_character(
                character_id, statuses=(GoalStatus.ACTIVE,),
            )
        except Exception:
            _LOGGER.exception("proactive: goal repository query failed")
            return ""
        if not goals:
            return ""
        lines = [
            f"- {g.content}（優先 {g.priority}）"
            for g in goals[:5]
        ]
        return "\n".join(lines)

    async def deliver_pre_composed(
        self,
        *,
        character_id: str,
        text: str,
        trigger: ProactiveTrigger,
        reason: str = "",
        attachments: tuple[OutboundAttachment, ...] = (),
        now: datetime | None = None,
    ) -> ProactiveAttempt:
        """Fan out a message whose text was decided elsewhere.

        Used by the ``PendingFollowUpDispatcher`` (and any other future
        caller that owns its own decision flow). Skips gate / decider /
        cooldown — the caller is responsible for not abusing it. Runs
        the same web + binding fan-out as the standard evaluate path,
        and writes a ``proactive_attempt`` row so the dispatch shows up
        in the audit log.

        Failure semantics mirror ``evaluate``: a partial fan-out (web
        succeeds, TG fails) still counts as SENT — the user got the
        message somewhere. Both sides failing logs ERRORED.
        """
        when = self._resolve_now(now)
        body = (text or "").strip()
        if not body:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.ERRORED,
                reason="empty pre-composed message",
                now=when,
            )
        character = await self._characters.get(character_id)
        if character is None:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.ERRORED,
                reason="character not found",
                now=when,
            )
        if (
            self._subscription_access_guard is not None
            and not await self._subscription_access_guard.is_character_allowed(
                character,
            )
        ):
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.DISABLED,
                reason="subscription is inactive",
                now=when,
            )

        eligible = await self._find_eligible_binding(character_id)
        web_enabled = bool(character.accepts_web_proactive)
        if eligible is None and not web_enabled:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.NO_BINDING,
                reason=(
                    "no binding has accepts_proactive=True and "
                    "accepts_web_proactive is False"
                ),
                now=when,
            )
        binding, account = eligible if eligible else (None, None)

        delivered = 0
        web_delivered = False
        external_delivered = False
        binding_id_for_log: str | None = None
        if web_enabled:
            try:
                await self._deliver_web(
                    character=character,
                    text=body,
                    attachments=attachments,
                    when=when,
                )
                delivered += 1
                web_delivered = True
            except Exception:
                _LOGGER.exception(
                    "pre-composed web delivery crashed character=%s",
                    character_id,
                )
        if binding is not None and account is not None:
            try:
                await self._deliver(
                    binding=binding, account=account, text=body,
                    attachments=attachments,
                    locale=await self._load_operator_language(character),
                )
                delivered += 1
                external_delivered = True
                binding_id_for_log = binding.id
            except Exception:
                _LOGGER.exception(
                    "pre-composed messaging delivery crashed character=%s",
                    character_id,
                )

        if delivered == 0:
            return await self._log(
                character_id=character_id,
                trigger=trigger,
                outcome=ProactiveOutcome.ERRORED,
                reason="delivery raised",
                binding_id=(binding.id if binding else None),
                message=body,
                now=when,
            )

        await self._notify_web_push(
            character=character,
            message=body,
            web_delivered=web_delivered,
            external_delivered=external_delivered,
        )

        return await self._log(
            character_id=character_id,
            trigger=trigger,
            outcome=ProactiveOutcome.SENT,
            reason=reason or f"pre-composed via {trigger.value}",
            binding_id=binding_id_for_log,
            message=body,
            now=when,
        )

    async def _notify_web_push(
        self,
        *,
        character: Character,
        message: str,
        web_delivered: bool,
        external_delivered: bool,
    ) -> None:
        if not web_delivered or self._notification_service is None:
            return
        try:
            await self._notification_service.notify_proactive(
                character,
                message,
                external_delivered=external_delivered,
            )
        except Exception:
            _LOGGER.exception(
                "proactive web push notification failed character=%s",
                character.id,
            )

    async def _log(
        self,
        *,
        character_id: str,
        trigger: ProactiveTrigger,
        outcome: ProactiveOutcome,
        reason: str,
        now: datetime,
        binding_id: str | None = None,
        message: str | None = None,
        metadata: dict | None = None,
    ) -> ProactiveAttempt:
        attempt = ProactiveAttempt.record(
            character_id=character_id,
            trigger=trigger,
            outcome=outcome,
            reason=reason,
            binding_id=binding_id,
            message=message,
            metadata=metadata,
            now=now,
        )
        try:
            await self._attempts.add(attempt)
        except Exception:
            _LOGGER.exception("failed to persist proactive attempt log")
        emotion_event_ids = await self._record_proactive_emotion_event(
            attempt=attempt,
        )
        if self._turn_recorder is not None:
            try:
                await self._turn_recorder.record(TurnRecordingDraft(
                    character_id=character_id,
                    kind="proactive",
                    prompt_pack_hash=(
                        self._prompt_pack_hash_provider()
                        if self._prompt_pack_hash_provider is not None else ""
                    ),
                    response_text=message or "",
                    post_turn_refs={
                        "proactive_attempt_id": attempt.id,
                        "trigger": trigger.value,
                        "outcome": outcome.value,
                        "reason": reason,
                        "binding_id": binding_id,
                        **(metadata or {}),
                        "emotion_event_ids": emotion_event_ids,
                    },
                ))
            except Exception:
                _LOGGER.exception("turn_recorder dispatch failed (kind=proactive)")
        return attempt

    async def _record_proactive_emotion_event(
        self, *, attempt: ProactiveAttempt,
    ) -> list[str]:
        """Mirror proactive audit outcomes into the emotion event stream.

        This is a low-intensity provenance event, not a semantic mood
        rewrite. Numeric deltas stay zero; later LLM-first layers can
        decide whether repeated blocked/sent attempts matter.
        """
        if self._emotion_events is None:
            return []
        try:
            event = EmotionEvent.new(
                character_id=attempt.character_id,
                operator_id=DEFAULT_OPERATOR_ID,
                cause_ref_kind=CAUSE_PROACTIVE_ATTEMPT,
                cause_ref_id=attempt.id,
                valence=_proactive_event_valence(attempt.outcome),
                arousal=0.1 if attempt.outcome != ProactiveOutcome.DISABLED else 0.0,
                intensity=_proactive_event_intensity(attempt.outcome),
                applied_to_state=False,
                emotion_label=f"proactive:{attempt.outcome.value}",
                evidence_quote=_proactive_event_evidence(attempt),
                decay_half_life_minutes=360,
                now=attempt.decided_at,
            )
            await self._emotion_events.add(event)
            return [event.id]
        except Exception:
            _LOGGER.exception(
                "emotion_event_repository.add failed (cause=proactive_attempt, attempt=%s)",
                attempt.id,
            )
            return []

    def _resolve_now(self, now: datetime | None) -> datetime:
        return ensure_utc(
            now if now is not None else (
                self._clock.now()
                if self._clock is not None
                else datetime.now(timezone.utc)
            ),
        )


def _compute_idle_minutes(
    character: Character, now: datetime,
) -> float | None:
    last = character.state.last_active_at
    if last is None:
        return None
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return max(0.0, (now - last).total_seconds() / 60.0)


def _count_unanswered_streak(
    recent_sent_attempts: tuple[ProactiveAttempt, ...],
    *,
    idle_minutes: float | None,
    now: datetime,
) -> int:
    """Leading run of SENT pushes the user has not replied to.

    Walks newest→oldest and counts attempts that went out *after* the
    user last spoke (so they remain unanswered), stopping at the first
    one the user replied to. Uses the exact same "replied iff the user
    spoke after the push" test as the prompt's per-message reply tag, so
    the streak count and the "（對方還沒回）" tags can never disagree.
    Returns 0 when there is no prior conversation (``idle_minutes`` is
    None) — silence from a user who never spoke is not "being ignored".
    """
    if idle_minutes is None:
        return 0
    streak = 0
    for attempt in recent_sent_attempts:  # newest first
        elapsed_min = (now - attempt.decided_at).total_seconds() / 60.0
        if idle_minutes < elapsed_min:
            # User spoke after this push → they replied → run ends.
            break
        streak += 1
    return streak


def _timezone_for_operator(
    operator: OperatorProfile | None,
    fallback: tzinfo,
) -> tzinfo:
    if operator is None:
        return fallback
    try:
        return timezone_for_id(getattr(operator, "timezone_id", None))
    except Exception:  # pragma: no cover - defensive
        return fallback


def _language_for_operator(operator: OperatorProfile | None) -> str:
    if operator is None:
        return "zh-TW"
    lang = (operator.primary_language or "").strip()
    return lang or "zh-TW"


def _format_memories(
    items: list[MemoryItem], *, now: datetime | None = None,
) -> str:
    if not items:
        return ""
    lines: list[str] = []
    for item in items[:6]:
        kind = item.kind.value if hasattr(item.kind, "value") else str(item.kind)
        lines.append(f"- [{kind}] {item.content}{_memory_recall_time_tag(item, now)}")
    return "\n".join(lines)


def _memory_recall_time_tag(item: MemoryItem, now: datetime | None) -> str:
    """Coarse "how long ago" suffix so the proactive judge knows whether
    a recalled fact is fresh enough to act on. Empty without a reference
    clock or on clock skew, leaving the line exactly as before."""
    if now is None:
        return ""
    created = getattr(item, "created_at", None)
    if created is None:
        return ""
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    elapsed_min = (now - created).total_seconds() / 60.0
    if elapsed_min < 0:
        return ""
    return f"（{format_relative_past_label(elapsed_min)}）"


def _format_intention_skip_reason(
    decision: ProactiveIntentionDecision,
) -> str:
    parts = ["intention skipped"]
    if decision.reason.strip():
        parts.append(decision.reason.strip())
    if decision.best_timing.strip():
        parts.append(f"best_timing={decision.best_timing.strip()}")
    if decision.risk.strip():
        parts.append(f"risk={decision.risk.strip()}")
    reason = " | ".join(parts)
    if len(reason) > 500:
        return reason[:497].rstrip() + "..."
    return reason


def _requires_user_started_interaction(trigger: ProactiveTrigger) -> bool:
    return trigger not in (
        ProactiveTrigger.PENDING_FOLLOW_UP,
        ProactiveTrigger.SCHEDULED_PROMISE,
    )


def _seed_allows_pre_message_proactive(
    seed: CharacterOperatorRelationshipSeed | None,
) -> bool:
    if seed is None:
        return False
    return bool(
        seed.proactive_permission
        and seed.proactive_cadence_hint.strip()
    )


def _proactive_event_valence(outcome: ProactiveOutcome) -> float:
    values = {
        ProactiveOutcome.SENT: 0.15,
        ProactiveOutcome.ERRORED: -0.2,
        ProactiveOutcome.INTENTION_SKIPPED: -0.05,
        ProactiveOutcome.DECIDER_SKIPPED: -0.05,
    }
    return values.get(outcome, 0.0)


def _proactive_event_intensity(outcome: ProactiveOutcome) -> float:
    values = {
        ProactiveOutcome.SENT: 0.25,
        ProactiveOutcome.ERRORED: 0.2,
        ProactiveOutcome.INTENTION_SKIPPED: 0.12,
        ProactiveOutcome.DECIDER_SKIPPED: 0.12,
        ProactiveOutcome.GATE_BLOCKED: 0.08,
        ProactiveOutcome.NO_BINDING: 0.08,
        ProactiveOutcome.DISABLED: 0.05,
    }
    return values.get(outcome, 0.1)


def _proactive_event_evidence(attempt: ProactiveAttempt) -> str:
    parts = [
        f"trigger={attempt.trigger.value}",
        f"outcome={attempt.outcome.value}",
    ]
    if attempt.reason:
        parts.append(f"reason={attempt.reason}")
    if attempt.message:
        parts.append(f"message={attempt.message[:80]}")
    return " | ".join(parts)[:240]


def _proactive_diversity_evidence(
    context: ProactiveContext,
) -> ReplyDiversityEvidence:
    lines = tuple(
        attempt.message or ""
        for attempt in context.recent_sent_attempts[:8]
        if attempt.message
    )
    return ReplyDiversityEvidence(
        assistant_line_count=len(lines),
        self_repetition_hint="",
        phrase_frequency_lines=(
            (
                "recent_sent_attempts 已提供近期主動訊息；請判斷是否重複同一目的或措辭。"
            )
            if lines else ()
        ),
    )


def _quality_gate_metadata(
    verdict: NoveltyVerdict | None,
    *,
    enabled: bool,
    retry_count: int,
) -> dict[str, object]:
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


def _register_profile_metadata(profile, *, enabled: bool) -> dict[str, object] | None:
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
        "error": metadata.get("error"),
    }


def _diversity_metadata(
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


# Type alias for the optional schedule resolver callback.
# Defined here (not as a Protocol import) to keep the file self-contained.
from typing import Awaitable, Callable  # noqa: E402

ScheduleResolver = Callable[
    [Character, datetime],
    Awaitable[tuple[object, list, object]],
]
